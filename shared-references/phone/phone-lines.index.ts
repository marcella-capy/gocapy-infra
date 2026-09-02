// Telnyx TeXML responder for Capy per-principal phone lines.
// Inbound: ring the rep on duty (shift split, America/Chicago -- Ericka 00:00
// to 12:59, Jon 13:00 to 23:59), caller ID = the principal line; if they miss
// it, ring the off-duty rep as a backup leg; answered-then-finished -> hangup;
// both legs missed -> Marcella's recorded greeting (phone_greetings) or TTS
// fallback, then voicemail record.
// VOICEMAILS: re-hosted permanently (Telnyx URLs expire in ~10 min), logged
// to phone_voicemails, announced in the inbound Discord channel.
// EVERY CALL is logged to public.phone_calls (keyed on CallSid), announced ONCE
// in Discord with the caller resolved against Pipedrive, and written to the
// caller's Pipedrive record as a done activity. The announcement happens at the
// `call-status` stage (Telnyx TeXML status_callback) -- i.e. after the call is
// over, so nothing in the ringing path waits on Pipedrive or Discord.
// IMPORTANT: on action/status callbacks Telnyx does NOT echo the original
// line number as To (observed live 2026-08-21) -- every non-entry stage
// must trust the To/From threaded through OUR callback query string, and
// only fall back to the form values. The ring order and CallSid are threaded
// the same way, so a call that starts at 12:59 finishes on the shift it
// started on and its recording can find its call row.
// TRAP: the activity subject must NEVER match the call-task-scheduler's marker
// regex ^(.+?): Call ([123]) - .+ from .+$ -- that scheduler sweeps matching
// subjects and subscribes people to HotHawk voicemail sequences.
// Auth: shared token in the query string - Telnyx cannot send Supabase JWTs.

const TOKEN = "OBsSH4RJCI4vdc-vRx7LLXz88t5Dy0c2";
const SELF_URL =
  "https://qedvkecwtooqjoxkgrme.supabase.co/functions/v1/phone-lines";
const DISCORD_WEBHOOK =
  "https://discord.com/api/webhooks/1473044894464610589/S6HjSesnsr8lj0n_HQVUu6fPEtB5BbmabObEFisf9wki-Uhs1NL3LLcOPjwPjKMzh9T4";

const RECORD_MODE = false; // all 10 greetings recorded 2026-08-21
const ADMINS = ["+19495245765", "+12247279327"]; // Marcella: GV work line + cell

const JON = "+12625013771";
const ERICKA = "+19492099625";
const RING = [JON, ERICKA]; // fallback only: if the clock is unreadable, both still ring
const SHIFT_TZ = "America/Chicago";
const SWITCH_HOUR = 13; // Ericka 00:00-12:59 CT, Jon 13:00-23:59 CT

// Pipedrive owner ids, for assigning the logged call to whoever was on duty.
const PD_OWNER: Record<string, number> = { [ERICKA]: 23490137, [JON]: 20845253 };

// Local hour in SHIFT_TZ. Named zone, never a fixed offset, so DST follows itself.
function shiftHour(d: Date = new Date()): number | null {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: SHIFT_TZ,
      hour: "numeric",
      hourCycle: "h23",
    }).formatToParts(d);
    const n = parseInt(parts.find((p) => p.type === "hour")?.value ?? "", 10);
    return Number.isNaN(n) ? null : n % 24; // some ICU builds emit "24" at midnight
  } catch (_e) {
    return null;
  }
}

// [on duty, backup] for the given moment.
function shift(d: Date = new Date()): string[] {
  const h = shiftHour(d);
  if (h === null) return RING;
  return h < SWITCH_HOUR ? [ERICKA, JON] : [JON, ERICKA];
}

function who(n: string): string {
  return n === ERICKA ? "Ericka" : n === JON ? "Jon" : n;
}

type Line = { name: string; persona: string; ring?: string[] };

const LINES: Record<string, Line> = {
  "+19495932145": { name: "Patriot Forge", persona: "Juliana" },
  "+19494090482": { name: "VRC", persona: "Larissa" },
  "+19493126568": { name: "Alpha Grainger", persona: "Diana" },
  "+19494090384": { name: "Tech-Max", persona: "Stephanie" },
  "+19495932811": { name: "LNP Machining", persona: "Sofia" },
  "+19495932774": { name: "Franklin Casting", persona: "Camila" },
  "+19495932822": { name: "General Foundry", persona: "Luciana" },
  "+19495932786": { name: "Shellcast", persona: "Luiza" },
  "+19495932799": { name: "Harvey Vogel", persona: "Julia" },
  "+19495932812": { name: "A.T. Wall", persona: "Karine" },
};

const SB_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const SB_HEADERS = {
  apikey: SB_KEY,
  Authorization: `Bearer ${SB_KEY}`,
  "Content-Type": "application/json",
};

// Shared with the email-ops-bridge function in this same project.
const PD_TOKEN = Deno.env.get("PIPEDRIVE_API_TOKEN") ?? "";
const PD_DOMAIN = Deno.env.get("PIPEDRIVE_DOMAIN") ?? "capy";
const PD_API = `https://${PD_DOMAIN}.pipedrive.com/api/v1`;

async function getGreetingUrl(to: string): Promise<string | null> {
  const r = await fetch(
    `${SB_URL}/rest/v1/phone_greetings?to_number=eq.${encodeURIComponent(to)}&select=recording_url`,
    { headers: SB_HEADERS },
  );
  if (!r.ok) return null;
  const rows = await r.json();
  return rows[0]?.recording_url ?? null;
}

async function rehost(telnyxUrl: string, bucket: string, path: string): Promise<string | null> {
  try {
    const audio = await fetch(telnyxUrl);
    if (!audio.ok) return null;
    const bytes = new Uint8Array(await audio.arrayBuffer());
    const up = await fetch(`${SB_URL}/storage/v1/object/${bucket}/${path}`, {
      method: "POST",
      headers: { ...SB_HEADERS, "Content-Type": "audio/mpeg", "x-upsert": "true" },
      body: bytes,
    });
    if (!up.ok) return null;
    return `${SB_URL}/storage/v1/object/public/${bucket}/${path}`;
  } catch (_e) {
    return null;
  }
}

async function saveGreeting(to: string, telnyxUrl: string): Promise<void> {
  const path = `${to.replace(/[^0-9]/g, "")}.mp3`;
  const finalUrl = (await rehost(telnyxUrl, "phone-greetings", path)) ?? telnyxUrl;
  await fetch(`${SB_URL}/rest/v1/phone_greetings`, {
    method: "POST",
    headers: { ...SB_HEADERS, Prefer: "resolution=merge-duplicates" },
    body: JSON.stringify([
      { to_number: to, recording_url: finalUrl, updated_at: new Date().toISOString() },
    ]),
  });
}

// ---------------------------------------------------------------- call log --

// Upsert on the CallSid primary key. PostgREST only updates the columns present
// in the body, so each handler writes its own fields without clobbering others.
async function upsertCall(row: Record<string, unknown>): Promise<void> {
  try {
    await fetch(`${SB_URL}/rest/v1/phone_calls`, {
      method: "POST",
      headers: {
        ...SB_HEADERS,
        Prefer: "resolution=merge-duplicates,return=minimal",
      },
      body: JSON.stringify([row]),
    });
  } catch (_e) { /* the call itself must never fail on bookkeeping */ }
}

async function getCall(sid: string): Promise<Record<string, unknown> | null> {
  try {
    const r = await fetch(
      `${SB_URL}/rest/v1/phone_calls?call_sid=eq.${encodeURIComponent(sid)}&select=*`,
      { headers: SB_HEADERS },
    );
    if (!r.ok) return null;
    const rows = await r.json();
    return rows[0] ?? null;
  } catch (_e) {
    return null;
  }
}

// ---------------------------------------------------------------- pipedrive --

type Match = {
  kind: "person" | "org" | "ambiguous" | "none";
  count: number;
  personId?: number;
  personName?: string;
  orgId?: number;
  orgName?: string;
};

// Resolve a caller by phone. Pipedrive normalizes phone search internally, so
// the E.164 number Telnyx gives us matches records stored as "(315) 956-2592",
// "6307736600" or "269-649-0200 x111" alike -- no local index needed.
// A number shared across a switchboard can hit many people (worst case here:
// 177), so a person is only named when exactly one matches.
async function pdLookup(from: string): Promise<Match> {
  if (!from || !PD_TOKEN) return { kind: "none", count: 0 };
  try {
    const url = new URL(`${PD_API}/persons/search`);
    url.searchParams.set("term", from);
    url.searchParams.set("fields", "phone");
    url.searchParams.set("limit", "50");
    url.searchParams.set("api_token", PD_TOKEN);
    const r = await fetch(url.toString());
    if (!r.ok) return { kind: "none", count: 0 };
    const body = await r.json();
    const items = (body?.data?.items ?? []) as Array<{ item: Record<string, any> }>;

    // Dedupe: the same person can come back more than once.
    const byId = new Map<number, Record<string, any>>();
    for (const it of items) {
      const p = it?.item;
      if (p?.id) byId.set(Number(p.id), p);
    }
    const people = [...byId.values()];
    if (people.length === 0) return { kind: "none", count: 0 };

    if (people.length === 1) {
      const p = people[0];
      return {
        kind: "person",
        count: 1,
        personId: Number(p.id),
        personName: p.name ?? undefined,
        orgId: p.organization?.id ? Number(p.organization.id) : undefined,
        orgName: p.organization?.name ?? undefined,
      };
    }

    // Several people on one number. If they all sit at the same organization we
    // still know the company; otherwise we know nothing and say so.
    const orgIds = new Set(
      people.map((p) => (p.organization?.id ? Number(p.organization.id) : 0)),
    );
    if (orgIds.size === 1 && !orgIds.has(0)) {
      const p = people[0];
      return {
        kind: "org",
        count: people.length,
        orgId: Number(p.organization.id),
        orgName: p.organization?.name ?? undefined,
      };
    }
    return { kind: "ambiguous", count: people.length };
  } catch (_e) {
    return { kind: "none", count: 0 };
  }
}

// v1 deliberately: v2 POST /activities rejects person_id (wants participants)
// and drops the note field. Same choice email-ops-bridge made.
async function pdCreateActivity(input: {
  subject: string;
  type: string;
  note: string;
  personId?: number;
  orgId?: number;
  ownerId: number;
  when: Date;
}): Promise<number | null> {
  if (!PD_TOKEN) return null;
  if (!input.personId && !input.orgId) return null; // nothing to attach it to
  try {
    const url = new URL(`${PD_API}/activities`);
    url.searchParams.set("api_token", PD_TOKEN);
    const iso = input.when.toISOString();
    const body: Record<string, unknown> = {
      subject: input.subject.slice(0, 255),
      type: input.type,
      done: 1,
      note: input.note.slice(0, 8000),
      owner_id: input.ownerId,
      due_date: iso.slice(0, 10),
      due_time: iso.slice(11, 16),
    };
    if (input.personId) body.person_id = input.personId;
    if (input.orgId) body.org_id = input.orgId;
    const r = await fetch(url.toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      console.log(`pd activity failed: ${r.status} ${(await r.text()).slice(0, 200)}`);
      return null;
    }
    const out = await r.json();
    return out?.data?.id ? Number(out.data.id) : null;
  } catch (e) {
    console.log(`pd activity error: ${e}`);
    return null;
  }
}

async function pdUpdateActivity(id: number, patch: Record<string, unknown>): Promise<void> {
  if (!PD_TOKEN || !id) return;
  try {
    const url = new URL(`${PD_API}/activities/${id}`);
    url.searchParams.set("api_token", PD_TOKEN);
    await fetch(url.toString(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
  } catch (_e) { /* the ping already went out; the note is a nicety */ }
}

// ------------------------------------------------------------------ discord --

// wait=true so Discord returns the message, whose id lets a later voicemail
// edit this same message instead of posting a second one.
async function discordPost(content: string): Promise<string | null> {
  try {
    const r = await fetch(`${DISCORD_WEBHOOK}?wait=true`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    if (!r.ok) return null;
    const m = await r.json();
    return m?.id ?? null;
  } catch (_e) {
    return null;
  }
}

async function discordPatch(messageId: string, content: string): Promise<boolean> {
  try {
    const r = await fetch(`${DISCORD_WEBHOOK}/messages/${messageId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    return r.ok;
  } catch (_e) {
    return false;
  }
}

// ------------------------------------------------------------------ display --

function pretty(n: string): string {
  const d = (n || "").replace(/\D/g, "");
  const t = d.length === 11 && d.startsWith("1") ? d.slice(1) : d;
  return t.length === 10
    ? `+1 (${t.slice(0, 3)}) ${t.slice(3, 6)}-${t.slice(6)}`
    : (n || "unknown number");
}

function hms(s: number): string {
  if (!s || s < 0) return "";
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}

function callerLabel(m: Match): string {
  if (m.kind === "person") {
    return `**${m.personName ?? "unnamed contact"}**` +
      (m.orgName ? ` — ${m.orgName}` : "");
  }
  if (m.kind === "org") {
    return `someone at **${m.orgName}** (${m.count} contacts share this number)`;
  }
  if (m.kind === "ambiguous") {
    return `**unidentified** — ${m.count} Pipedrive contacts share this number`;
  }
  return "**unknown caller** — not in Pipedrive";
}

function renderPing(o: {
  line: Line;
  from: string;
  outcome: string;
  seconds: number;
  match: Match;
  voicemailUrl?: string | null;
  voicemailSecs?: number;
}): string {
  const head = o.outcome === "answered"
    ? "\u{1F4DE} **Call answered**"
    : o.outcome === "voicemail"
    ? "\u{1F4E7} **Voicemail**"
    : "\u{1F4F5} **Missed call**";
  const lines = [
    `${head} on the **${o.line.name}** line`,
    `From ${callerLabel(o.match)}`,
    `${pretty(o.from)}${o.seconds ? ` · ${hms(o.seconds)}` : ""}`,
  ];
  if (o.voicemailUrl) {
    lines.push(
      `\u{1F3A7} [Listen](${o.voicemailUrl})${o.voicemailSecs ? ` · ${hms(o.voicemailSecs)}` : ""}`,
    );
  }
  return lines.join("\n");
}

// House style borrowed from email-ops-bridge: "[Display] Label: subject".
// Deliberately NOT "<Display>: Call <n> - <Last> from <Org>" -- that pattern is
// the call-task-scheduler's machine marker and gets swept by its rotation logic.
function activitySubject(line: Line, m: Match, from: string): string {
  const whoStr = m.kind === "person"
    ? `${m.personName}${m.orgName ? ` (${m.orgName})` : ""}`
    : m.kind === "org"
    ? m.orgName ?? pretty(from)
    : pretty(from);
  return `[${line.name}] Inbound call: ${whoStr}`;
}

const ACTIVITY_TYPE: Record<string, string> = {
  answered: "call_logged",
  missed: "unanswered_call",
  voicemail: "voicemail",
};

// ------------------------------------------------------------ announce once --

async function announceCall(sid: string, to: string, from: string, seconds: number): Promise<void> {
  const row = await getCall(sid);
  if (row?.notified_at) return; // already announced; nothing to do

  const line = LINES[to];
  if (!line) return;
  const answered = row?.answered === true;
  const outcome = answered ? "answered" : "missed";
  const when = new Date();

  const match = await pdLookup(from);
  const ownerId = PD_OWNER[shift(when)[0]] ?? PD_OWNER[ERICKA];

  const msgId = await discordPost(
    renderPing({ line, from, outcome, seconds, match }),
  );

  const note = [
    `Inbound call to the ${line.name} line (${pretty(to)}).`,
    `Caller: ${pretty(from)}.`,
    outcome === "answered"
      ? `Answered${seconds ? `, ${hms(seconds)}` : ""}.`
      : `Not answered${seconds ? ` after ${hms(seconds)}` : ""}; no message left.`,
    `Logged automatically by the phone lines.`,
  ].join(" ");

  const actId = await pdCreateActivity({
    subject: activitySubject(line, match, from),
    type: ACTIVITY_TYPE[outcome],
    note,
    personId: match.personId,
    orgId: match.orgId,
    ownerId,
    when,
  });

  await upsertCall({
    call_sid: sid,
    to_number: to,
    principal: line.name,
    from_number: from,
    outcome,
    duration_s: seconds || null,
    pd_match: match.kind,
    pd_person_id: match.personId ?? null,
    pd_person_name: match.personName ?? null,
    pd_org_id: match.orgId ?? null,
    pd_org_name: match.orgName ?? null,
    pd_activity_id: actId,
    discord_message_id: msgId,
    notified_at: when.toISOString(),
  });
}

async function saveVoicemail(
  to: string,
  from: string,
  telnyxUrl: string,
  duration: string,
  sid: string,
): Promise<void> {
  const line = LINES[to];
  const path = `${crypto.randomUUID()}.mp3`;
  const finalUrl = (await rehost(telnyxUrl, "phone-voicemails", path)) ?? telnyxUrl;
  const secs = parseInt(duration || "0", 10) || 0;

  await fetch(`${SB_URL}/rest/v1/phone_voicemails`, {
    method: "POST",
    headers: SB_HEADERS,
    body: JSON.stringify([{
      to_number: to,
      principal: line?.name ?? "Unknown line",
      from_number: from,
      recording_url: finalUrl,
      duration_s: secs || null,
      call_sid: sid || null,
    }]),
  });

  const row = sid ? await getCall(sid) : null;
  const match: Match = row?.pd_match
    ? {
      kind: row.pd_match as Match["kind"],
      count: 0,
      personId: (row.pd_person_id as number) ?? undefined,
      personName: (row.pd_person_name as string) ?? undefined,
      orgId: (row.pd_org_id as number) ?? undefined,
      orgName: (row.pd_org_name as string) ?? undefined,
    }
    : await pdLookup(from);

  const body = renderPing({
    line: line ?? { name: to, persona: "our team" },
    from,
    outcome: "voicemail",
    seconds: 0,
    match,
    voicemailUrl: finalUrl,
    voicemailSecs: secs,
  });

  // Upgrade the message this call already has, rather than posting a second one.
  const existingMsg = row?.discord_message_id as string | undefined;
  let msgId = existingMsg ?? null;
  if (!existingMsg || !(await discordPatch(existingMsg, body))) {
    msgId = await discordPost(body);
  }

  // Same for the activity: a missed call that turns out to have a message is
  // still one call, so retype it instead of logging a second one.
  const actId = row?.pd_activity_id as number | undefined;
  if (actId) {
    await pdUpdateActivity(actId, {
      type: "voicemail",
      subject: activitySubject(line ?? { name: "Capy", persona: "our team" }, match, from),
      note: `Inbound call to the ${line?.name ?? to} line (${pretty(to)}). ` +
        `Caller: ${pretty(from)}. Left a voicemail${secs ? ` of ${hms(secs)}` : ""}: ${finalUrl}`,
    });
  } else if (line) {
    const when = new Date();
    const newId = await pdCreateActivity({
      subject: activitySubject(line, match, from),
      type: "voicemail",
      note: `Inbound call to the ${line.name} line (${pretty(to)}). Caller: ${pretty(from)}. ` +
        `Left a voicemail${secs ? ` of ${hms(secs)}` : ""}: ${finalUrl}`,
      personId: match.personId,
      orgId: match.orgId,
      ownerId: PD_OWNER[shift(when)[0]] ?? PD_OWNER[ERICKA],
      when,
    });
    if (sid) {
      await upsertCall({
        call_sid: sid,
        to_number: to,
        principal: line.name,
        from_number: from,
        pd_match: match.kind,
        pd_person_id: match.personId ?? null,
        pd_person_name: match.personName ?? null,
        pd_org_id: match.orgId ?? null,
        pd_org_name: match.orgName ?? null,
        pd_activity_id: newId,
      });
    }
  }

  if (sid) {
    await upsertCall({
      call_sid: sid,
      outcome: "voicemail",
      reached_voicemail: true,
      discord_message_id: msgId,
      notified_at: new Date().toISOString(),
    });
  }
}

function ttsGreeting(line: Line): string {
  return (
    `Hi, you've reached ${line.persona} with ${line.name}. ` +
    "I can't take your call right now. Please leave your name, number, and " +
    "company after the tone, and I'll get back to you shortly."
  );
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;");
}

function xml(inner: string): Response {
  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?><Response>${inner}</Response>`,
    { headers: { "Content-Type": "application/xml" } },
  );
}

// One leg of the ring: dial order[leg], and hand the rest of the order to the
// action callback so the next leg does not recompute the shift mid-call.
function dialLeg(to: string, from: string, order: string[], leg: number): Response {
  const next = esc(
    `${SELF_URL}?token=${TOKEN}&stage=after-dial&leg=${leg + 1}` +
      `&order=${encodeURIComponent(order.join(","))}` +
      `&To=${encodeURIComponent(to)}&From=${encodeURIComponent(from)}`,
  );
  return xml(
    `<Dial callerId="${to || "+19495932145"}" timeout="15" action="${next}">` +
      `<Number>${order[leg]}</Number></Dial>`,
  );
}

Deno.serve(async (req: Request) => {
  const url = new URL(req.url);
  if (url.searchParams.get("token") !== TOKEN) {
    return new Response("forbidden", { status: 403 });
  }

  let form = new URLSearchParams();
  if (req.method === "POST") form = new URLSearchParams(await req.text());

  const stage = url.searchParams.get("stage") ?? "entry";

  // Read-only: proves the shift boundary without waiting for the clock, and
  // reports whether the Pipedrive credential is visible (never its value).
  if (stage === "schedule" || stage === "probe") {
    const now = new Date();
    const table = Array.from({ length: 24 }, (_, h) => ({
      hour: h,
      onDuty: who(h < SWITCH_HOUR ? ERICKA : JON),
      backup: who(h < SWITCH_HOUR ? JON : ERICKA),
    }));
    return new Response(
      JSON.stringify({
        tz: SHIFT_TZ,
        switchHour: SWITCH_HOUR,
        nowUtc: now.toISOString(),
        nowLocal: now.toLocaleString("en-US", { timeZone: SHIFT_TZ }),
        localHour: shiftHour(now),
        ringNow: shift(now).map(who),
        pipedriveTokenPresent: PD_TOKEN.length > 0,
        pipedriveDomain: PD_DOMAIN,
        pdOwnerOnDuty: PD_OWNER[shift(now)[0]] ?? null,
        supabaseKeyPresent: SB_KEY.length > 0,
        table,
      }, null, 2),
      { headers: { "Content-Type": "application/json" } },
    );
  }

  // Read-only: resolve a number against Pipedrive without writing anything.
  if (stage === "lookup") {
    const q = url.searchParams.get("number") ?? "";
    const m = await pdLookup(q);
    return new Response(
      JSON.stringify({ number: q, pretty: pretty(q), match: m, label: callerLabel(m) }, null, 2),
      { headers: { "Content-Type": "application/json" } },
    );
  }

  // Entry: trust the inbound webhook's form. Every other stage: trust OUR
  // query-threaded values first (Telnyx's callback To is not the line).
  const to = stage === "entry"
    ? (form.get("To") ?? url.searchParams.get("To") ?? "")
    : (url.searchParams.get("To") ?? form.get("To") ?? "");
  const from = stage === "entry"
    ? (form.get("From") ?? "")
    : (url.searchParams.get("From") ?? form.get("From") ?? "");
  const sid = url.searchParams.get("CallSid") ?? form.get("CallSid") ?? "";
  const line = LINES[to] ?? { name: "Capy", persona: "our team" };

  console.log(JSON.stringify({
    stage, resolvedTo: to, resolvedFrom: from, sid,
    formTo: form.get("To"), formFrom: form.get("From"),
    dialStatus: form.get("DialCallStatus"),
    leg: url.searchParams.get("leg"),
  }));

  // Telnyx TeXML application status_callback: fires once when a call ends.
  // This is the only announcement point, and it runs after the call is over,
  // so nothing in the ringing path ever waits on Pipedrive or Discord.
  if (stage === "call-status") {
    const cTo = form.get("To") ?? "";
    const cFrom = form.get("From") ?? "";
    const cSid = form.get("CallSid") ?? "";
    const secs = parseInt(form.get("CallDuration") ?? "0", 10) || 0;
    console.log(JSON.stringify({
      stage: "call-status-raw",
      form: Object.fromEntries(form.entries()),
    }));
    // The outbound legs to Jon and Ericka ride the same connection; only the
    // inbound leg lands on one of our ten lines.
    if (!cSid || !LINES[cTo]) return new Response("ignored", { status: 200 });
    await announceCall(cSid, cTo, cFrom, secs);
    return new Response("ok", { status: 200 });
  }

  if (stage === "greeting-saved") {
    const rec = form.get("RecordingUrl") ?? "";
    if (to && rec) await saveGreeting(to, rec);
    return xml("");
  }

  if (stage === "voicemail-saved") {
    const rec = form.get("RecordingUrl") ?? "";
    const dur = form.get("RecordingDuration") ?? "";
    if (to && rec) await saveVoicemail(to, from, rec, dur, sid);
    return xml("");
  }

  if (stage === "after-dial") {
    const status = form.get("DialCallStatus") ?? "";

    if (status === "completed") {
      // A human picked up. Record it so the end-of-call announcement calls this
      // an answered call rather than a missed one. Awaited deliberately: the
      // caller is already hanging up, so the few ms cost nothing.
      if (sid && LINES[to]) {
        await upsertCall({
          call_sid: sid, to_number: to, principal: LINES[to].name,
          from_number: from, answered: true,
        });
      }
      return xml("<Hangup/>");
    }

    // Not answered: try the backup leg before dropping to voicemail.
    const leg = parseInt(url.searchParams.get("leg") ?? "1", 10) || 1;
    const threaded = (url.searchParams.get("order") ?? "").split(",").filter(Boolean);
    const order = threaded.length ? threaded : (line.ring ?? shift());
    if (leg < order.length) return dialLeg(to, from, order, leg);

    if (sid && LINES[to]) {
      await upsertCall({
        call_sid: sid, to_number: to, principal: LINES[to].name,
        from_number: from, reached_voicemail: true,
      });
    }

    const rec = await getGreetingUrl(to);
    const greet = rec
      ? `<Play>${esc(rec)}</Play>`
      : `<Say>${ttsGreeting(line)}</Say>`;
    const cb = esc(
      `${SELF_URL}?token=${TOKEN}&stage=voicemail-saved&To=${encodeURIComponent(to)}` +
        `&From=${encodeURIComponent(from)}&CallSid=${encodeURIComponent(sid)}`,
    );
    return xml(
      `${greet}<Record maxLength="120" playBeep="true" ` +
        `recordingStatusCallback="${cb}" recordingStatusCallbackMethod="POST"/>`,
    );
  }

  // entry
  if (RECORD_MODE && ADMINS.includes(from)) {
    const cb = esc(
      `${SELF_URL}?token=${TOKEN}&stage=greeting-saved&To=${encodeURIComponent(to)}`,
    );
    return xml(
      `<Say>Recording the voicemail greeting for ${line.name}. ` +
        "Speak after the beep, then press pound or hang up.</Say>" +
        `<Record maxLength="90" playBeep="true" finishOnKey="#" ` +
        `recordingStatusCallback="${cb}" recordingStatusCallbackMethod="POST"/>`,
    );
  }

  return dialLeg(to, from, line.ring ?? shift(), 0);
});
