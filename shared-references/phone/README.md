# Capy principal phone lines

`phone-lines.index.ts` is the reviewable copy of the Telnyx call-routing logic.
It is NOT built from here - the running copy is a deployed Supabase edge function,
and this file exists so the rule that decides who answers the phone is readable
and diffable outside a cloud dashboard.

- Supabase project `qedvkecwtooqjoxkgrme` (email-ops-bridge), function `phone-lines`
- Telnyx TeXML app "Capy principal lines" points its voice_url at that function,
  so routing changes never need a Telnyx-side edit
- Deploy: Supabase MCP `deploy_edge_function`. **`verify_jwt` MUST stay `false`** -
  Telnyx cannot send Supabase JWTs, the function authenticates on a shared token in
  the query string instead. The deploy tool defaults `verify_jwt` to `true`; letting
  it default takes all ten lines dead.
- After deploying, edit this file to match. If they drift, the deployed one is real.

## Who rings (v10, 2026-08-31)

Shift split on `America/Chicago` (named zone, so DST follows itself):

| Central time | Rings first | Backup if no answer |
|---|---|---|
| 12:00am - 12:59pm | Ericka | Jon |
| 1:00pm - 11:59pm | Jon | Ericka |

Each leg rings 15s. Both missed -> the principal's recorded greeting, then voicemail,
which is re-hosted to Supabase storage, logged to `phone_voicemails`, and posted to
Discord. Before v10 both phones rang simultaneously, 24/7.

The ring order is threaded through our own callback query string (`&order=`), not
recomputed per leg, so a call that starts at 12:59pm still reaches Jon on leg 2
instead of ringing Ericka twice.

## Checking it without waiting for the clock

`GET <function-url>?token=<token>&stage=schedule` returns the current local hour, who
is on duty right now, and the full 24-hour table. Read-only, token-guarded.

## Standing dependencies

- Both targets are Google Voice numbers: screening must stay **off** and "show
  caller's number" **on**, or GV answers the leg itself, Telnyx records the call as
  `completed`, and the caller lands in a personal voicemail box instead of the
  principal's greeting.
- The 15s leg timeout is deliberately shorter than GV's own voicemail pickup. The two
  legs are sequential 15s timers, not one 30s timer.

## What happens after a call (v11, 2026-09-01)

The TeXML app's `status_callback` points at `&stage=call-status`, which fires once when a call ends.
That is the only announcement point, so the ringing path never waits on Pipedrive or Discord.

Every inbound call gets:
- a row in `public.phone_calls`, keyed on the Telnyx **CallSid** (`notified_at` is the announce-once guard)
- one Discord message naming the caller
- one done activity on the caller's Pipedrive record

The caller is resolved with `GET /v1/persons/search?term=<E.164>&fields=phone`. Pipedrive normalizes
phone search on its side, so the raw E.164 from Telnyx matches records stored as `(315) 956-2592`,
`6307736600`, or `269-649-0200 x111` alike - there is no local phone index and none is needed.

A number can belong to several contacts, so the match is deliberate:

| search result | what we say | what we write |
|---|---|---|
| exactly one person | names them | activity on the person |
| several, all at one org | "someone at <Org>" | activity on the org |
| several across orgs | names nobody | nothing |
| none | bare number | nothing |

Verified against real data: a 69-person single-org switchboard resolves to "someone at SpaceX"; the
177-person number that spans 174 organizations resolves to nobody. A person is never guessed.

Activity types are Pipedrive built-ins - `call_logged`, `unanswered_call`, `voicemail` - owned by the
rep on duty when the call came in. A voicemail EDITS the Discord message the call already has and
retypes the same activity, so one call is one message and one activity, never two.

**TRAP:** the activity subject must never match the call-task-scheduler's marker regex
`^(.+?): Call ([123]) - .+ from .+$`. That scheduler sweeps matching subjects and subscribes people to
HotHawk voicemail sequences. The subject used here is `[<Principal>] Inbound call: <Who>`.

Read-only debug stages, both token-guarded and safe to hit any time:
- `&stage=probe` - shift boundary, on-duty rep, whether the Pipedrive credential is visible
- `&stage=lookup&number=+1...` - resolve a number against Pipedrive, writes nothing
