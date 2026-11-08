/* KeyPublic game server. One Durable Object per room; every phone holds a WebSocket to it.
   The server owns the game. Each connection is only ever sent what its role may see. */
import { DurableObject } from "cloudflare:workers";
import { WORDS } from "./words.js";

const MAX_PLAYERS = 500;              // change this to cap a room
const IDLE_MS = 12 * 60 * 60 * 1000;  // rooms clean themselves up after this long untouched
const THRESHOLDS = ["1", "10", "25", "50", "75", "all"];
const SECONDS = [60, 120, 180];
const EYES = ["eyes1", "target", "eyes2", "clue"];

const ALLOWED_ORIGIN = /^https:\/\/bren\.place$|^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return new Response("ok");
    const m = /^\/room\/([A-Z]{4})$/.exec(url.pathname);
    if (!m) return new Response("Not found", { status: 404 });
    if (req.headers.get("Upgrade") !== "websocket") return new Response("Expected a WebSocket", { status: 426 });
    if (!ALLOWED_ORIGIN.test(req.headers.get("Origin") || "")) return new Response("Forbidden", { status: 403 });
    return env.ROOMS.get(env.ROOMS.idFromName(m[1])).fetch(req);
  },
};

function shuffle(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

export class Room extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.S = null;
    this.saveTimer = null;
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
    ctx.blockConcurrencyWhile(async () => { this.S = (await ctx.storage.get("s")) || null; });
  }

  /* ---------- Connections ---------- */
  async fetch(req) {
    const url = new URL(req.url);
    const code = url.pathname.slice(-4);
    const token = url.searchParams.get("token") || "";
    const create = url.searchParams.get("create") === "1";
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.ctx.acceptWebSocket(server);

    const refuse = reason => {
      server.send(JSON.stringify({ t: "fatal", code: reason }));
      server.close(4000, reason);
      return new Response(null, { status: 101, webSocket: client });
    };
    if (!/^[a-z0-9]{16,40}$/.test(token)) return refuse("badtoken");
    if (create) {
      if (this.S && this.S.host !== token) return refuse("taken");
      if (!this.S) this.S = this.newRoom(code, token);
    } else if (!this.S) {
      return refuse("nogame");
    }
    server.serializeAttachment({ token });
    this.send(server, token);
    this.touch();
    this.sendHost();
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, raw) {
    if (!this.S || typeof raw !== "string") return;
    const at = ws.deserializeAttachment();
    if (!at) return;
    let m;
    try { m = JSON.parse(raw); } catch { return; }
    const fn = this.actions[m.a];
    if (!fn) return;
    const problem = fn.call(this, at.token, m);
    if (problem) ws.send(JSON.stringify({ t: "error", msg: problem }));
    this.touch();
  }

  async webSocketClose(ws, code, reason, clean) {
    try { ws.close(1000); } catch {}
    this.sendHost();
  }
  async webSocketError() { this.sendHost(); }

  async alarm() {
    const S = this.S;
    if (!S) return;
    const now = Date.now();
    if (S.phase === "timing" && S.timer.endsAt && S.timer.endsAt <= now + 50) {
      this.alarmAt = null;
      this.timeUp();
      this.touch();
      return;
    }
    this.alarmAt = null;
    if (now - S.touched >= IDLE_MS) {
      this.ctx.getWebSockets().forEach(w => { try { w.close(1000, "expired"); } catch {} });
      this.S = null;
      await this.ctx.storage.deleteAll();
      return;
    }
    this.touch();
  }

  touch() {
    const S = this.S;
    if (!S) return;
    S.touched = Date.now();
    const at = S.phase === "timing" && S.timer.endsAt ? S.timer.endsAt : S.touched + IDLE_MS;
    /* Alarm writes count against the free tier, so only move it when it matters. */
    if (at !== this.alarmAt && (S.phase === "timing" || !this.alarmAt || Math.abs(at - this.alarmAt) > 3600000)) {
      this.alarmAt = at;
      this.ctx.storage.setAlarm(at);
    }
    if (!this.saveTimer) {
      this.saveTimer = setTimeout(() => {
        this.saveTimer = null;
        if (this.S) this.ctx.storage.put("s", this.S);
      }, 150);
    }
  }

  /* ---------- State ---------- */
  newRoom(code, host) {
    return {
      code, host, phase: "setup", threshold: "25", seconds: 120, players: {}, nextNum: 1, bob: null,
      round: 0, board: [], target: 0, clue: "", used: [], votes: {}, bobPick: null, result: null,
      timer: { msLeft: 120000, endsAt: null, started: false }, resume: null, touched: Date.now(),
    };
  }
  roleOf(t) {
    const S = this.S;
    if (t === S.host) return "alice";
    if (t === S.bob) return "bob";
    const p = S.players[t];
    return p && !p.left ? "mallory" : "visitor";
  }
  mallories() {
    const S = this.S;
    return Object.keys(S.players).filter(t => !S.players[t].left && t !== S.bob);
  }
  connected() {
    const out = new Set();
    for (const w of this.ctx.getWebSockets()) {
      const a = w.deserializeAttachment();
      if (a) out.add(a.token);
    }
    return out;
  }
  msLeft() {
    const t = this.S.timer;
    return t.endsAt ? Math.max(0, t.endsAt - Date.now()) : t.msLeft;
  }
  tally() {
    const c = Array(25).fill(0);
    for (const v of Object.values(this.S.votes)) c[v]++;
    return c;
  }

  view(t) {
    const S = this.S, role = this.roleOf(t), p = S.players[t];
    const v = {
      t: "state", code: S.code, phase: S.phase, round: S.round, role, num: p ? p.num : null, left: !!(p && p.left),
      joined: !!p, locked: S.round > 0, bobOpen: !S.bob, bobNum: S.bob && S.players[S.bob] ? S.players[S.bob].num : null,
      threshold: S.threshold, seconds: S.seconds, msLeft: this.msLeft(), running: !!S.timer.endsAt, started: S.timer.started,
      board: S.board, myVote: t in S.votes ? S.votes[t] : null, resuming: !!S.resume,
    };
    if (S.phase === "bob" || S.phase === "result") {
      v.tally = this.tally();
      v.voters = Object.keys(S.votes).length;
    }
    if (S.phase === "result") {
      v.result = S.result;
      v.intercepted = t in S.votes && S.votes[t] === S.result.target;
    }
    if (role === "alice") {
      const ms = this.mallories();
      v.swarmTotal = ms.length;
      v.swarmLocked = ms.filter(x => x in S.votes).length;
      const on = this.connected();
      v.swarmOnline = ms.filter(x => on.has(x)).length;
      v.bobOnline = !!S.bob && on.has(S.bob);
      if (S.phase === "result") v.swarmNums = ms.map(x => S.players[x].num).sort((a, b) => a - b);
      if (S.phase === "target") v.target = S.target;
      if (S.phase === "clue") v.clue = S.clue;
    }
    if (role === "bob" && ["clue", "ready", "timing", "swarm", "bob"].includes(S.phase)) v.clue = S.clue;
    return v;
  }
  send(ws, token) {
    try { ws.send(JSON.stringify(this.view(token))); } catch {}
  }
  broadcast() {
    for (const w of this.ctx.getWebSockets()) {
      const a = w.deserializeAttachment();
      if (a) this.send(w, a.token);
    }
  }
  sendTo(token) {
    for (const w of this.ctx.getWebSockets()) {
      const a = w.deserializeAttachment();
      if (a && a.token === token) this.send(w, token);
    }
  }
  sendHost() { if (this.S) this.sendTo(this.S.host); }

  /* ---------- Game flow ---------- */
  dealWords() {
    const S = this.S;
    let pool = WORDS.filter(w => !S.used.includes(w));
    if (pool.length < 26) { S.used = []; pool = WORDS.slice(); }
    shuffle(pool);
    S.board = pool.slice(0, 25);
    S.clue = pool[25];
    S.target = Math.floor(Math.random() * 25);
    S.used = S.used.concat(pool.slice(0, 26));
  }
  startRound(phase) {
    const S = this.S;
    S.round++;
    this.dealWords();
    S.votes = {}; S.bobPick = null; S.result = null; S.resume = null;
    S.timer = { msLeft: S.seconds * 1000, endsAt: null, started: false };
    S.phase = phase;
    this.broadcast();
  }
  timeUp() {
    const S = this.S;
    S.timer.msLeft = 0; S.timer.endsAt = null;
    S.phase = "swarm";
    if (!this.finishSwarmIfDone()) this.broadcast();
  }
  finishSwarmIfDone() {
    const S = this.S;
    if (S.phase !== "swarm") return false;
    if (this.mallories().every(t => t in S.votes)) { this.toBob(); return true; }
    return false;
  }
  toBob() { this.S.phase = "bob"; this.broadcast(); }
  finishRound() {
    const S = this.S;
    const tokens = Object.keys(S.votes);
    const hits = tokens.filter(t => S.votes[t] === S.target);
    const voters = tokens.length;
    let need;
    if (S.threshold === "1") need = 1;
    else if (S.threshold === "all") need = voters;
    else need = Math.max(1, Math.ceil(voters * Number(S.threshold) / 100));
    const foiled = voters > 0 && hits.length >= need;
    const bobRight = S.bobPick === S.target;
    S.result = {
      target: S.target, targetWord: S.board[S.target], clue: S.clue, bobPick: S.bobPick,
      bobWord: S.bobPick === null ? null : S.board[S.bobPick], bobRight, foiled, voters, need, threshold: S.threshold,
      interceptors: hits.map(t => S.players[t].num).sort((a, b) => a - b),
    };
    S.phase = "result";
    this.broadcast();
  }

  actions = {
    /* Anyone during setup */
    claimBob(t) {
      const S = this.S;
      if (S.phase !== "setup" || t === S.host) return;
      if (S.bob) return "Bob's already taken.";
      if (!S.players[t] || S.players[t].left) {
        const problem = this.addPlayer(t);
        if (problem) return problem;
      }
      S.bob = t;
      this.broadcast();
    },
    joinSwarm(t) {
      const S = this.S;
      if (S.phase !== "setup" || t === S.host || t === S.bob) return;
      if (S.players[t] && !S.players[t].left) return;
      const problem = this.addPlayer(t);
      if (problem) return problem;
      this.sendTo(t);
      this.sendHost();
    },
    /* Leaving: Mallories any time, Bob only before the game locks */
    leave(t) {
      const S = this.S, p = S.players[t];
      if (!p || p.left) return;
      if (t === S.bob) {
        if (S.phase !== "setup") return "Bob can't leave mid-game. Ask Alice to pick a new Bob.";
        S.bob = null;
        p.left = true;
        this.broadcast();
        return;
      }
      p.left = true;
      if (S.phase === "swarm" || S.phase === "setup") delete S.votes[t];
      this.sendTo(t);
      this.sendHost();
      this.finishSwarmIfDone();
    },
    /* Mallory */
    vote(t, m) {
      const S = this.S;
      if (S.phase !== "swarm" || this.roleOf(t) !== "mallory" || t in S.votes) return;
      if (!Number.isInteger(m.i) || m.i < 0 || m.i > 24) return;
      S.votes[t] = m.i;
      if (!this.finishSwarmIfDone()) { this.sendTo(t); this.sendHost(); }
    },
    /* Bob */
    bobPick(t, m) {
      const S = this.S;
      if (S.phase !== "bob" || t !== S.bob) return;
      if (!Number.isInteger(m.i) || m.i < 0 || m.i > 24) return;
      S.bobPick = m.i;
      this.finishRound();
    },
    /* Alice */
    setOpt(t, m) {
      const S = this.S;
      if (t !== S.host || S.phase !== "setup") return;
      if (THRESHOLDS.includes(m.threshold)) S.threshold = m.threshold;
      if (SECONDS.includes(m.seconds)) { S.seconds = m.seconds; S.timer.msLeft = m.seconds * 1000; }
      this.sendHost();
    },
    releaseBob(t) {
      const S = this.S;
      if (t !== S.host || S.phase !== "setup" || !S.bob) return;
      S.bob = null;
      this.broadcast();
    },
    deal(t) {
      const S = this.S;
      if (t !== S.host || S.phase !== "setup") return;
      if (!S.bob) return "A player needs to claim Bob first.";
      this.startRound("eyes1");
    },
    advance(t) {
      const S = this.S;
      if (t !== S.host) return;
      const next = { bobcall: "eyes1", eyes1: "target", target: "eyes2", eyes2: "clue" }[S.phase];
      if (next) { S.phase = next; this.broadcast(); return; }
      if (S.phase === "clue") {
        S.phase = S.resume || "ready";
        S.resume = null;
        this.broadcast();
      }
    },
    timerToggle(t) {
      const S = this.S;
      if (t !== S.host || !["ready", "timing"].includes(S.phase)) return;
      const T = S.timer;
      if (T.endsAt) { T.msLeft = this.msLeft(); T.endsAt = null; }
      else { T.endsAt = Date.now() + T.msLeft; T.started = true; S.phase = "timing"; }
      this.broadcast();
    },
    timerRestart(t) {
      const S = this.S;
      if (t !== S.host || !["timing", "swarm"].includes(S.phase) || !S.timer.started) return;
      S.votes = {};
      S.timer.msLeft = S.seconds * 1000;
      S.timer.endsAt = Date.now() + S.timer.msLeft;
      S.phase = "timing";
      this.broadcast();
    },
    timerEnd(t) {
      const S = this.S;
      if (t !== S.host || !["ready", "timing"].includes(S.phase) || !S.timer.started) return;
      this.timeUp();
    },
    resecret(t) {
      const S = this.S;
      if (t !== S.host || !["ready", "timing"].includes(S.phase)) return;
      if (S.timer.endsAt) { S.timer.msLeft = this.msLeft(); S.timer.endsAt = null; }
      S.resume = S.phase;
      S.phase = "eyes1";
      this.broadcast();
    },
    redeal(t) {
      const S = this.S;
      if (t !== S.host || !["eyes1", "target", "eyes2", "clue", "ready", "timing", "swarm", "bob"].includes(S.phase)) return;
      this.dealWords();
      S.votes = {}; S.bobPick = null; S.result = null; S.resume = null;
      S.timer = { msLeft: S.seconds * 1000, endsAt: null, started: false };
      S.phase = "eyes1";
      this.broadcast();
    },
    endSwarm(t) {
      const S = this.S;
      if (t !== S.host || S.phase !== "swarm") return;
      this.toBob();
    },
    endBob(t) {
      const S = this.S;
      if (t !== S.host || S.phase !== "bob") return;
      S.bobPick = null;
      this.finishRound();
    },
    next(t, m) {
      const S = this.S;
      if (t !== S.host || S.phase !== "result") return;
      const ms = this.mallories();
      let nb = S.bob;
      if (m.mode === "random") {
        if (!ms.length) return "There are no Mallories left to pick from.";
        const on = this.connected();
        const live = ms.filter(x => on.has(x));
        const pool = live.length ? live : ms;
        nb = pool[Math.floor(Math.random() * pool.length)];
      } else if (m.mode === "select") {
        nb = ms.find(x => S.players[x].num === m.num);
        if (!nb) return `There's no Mallory ${Number(m.num) || "with that number"} in the game.`;
      } else if (m.mode !== "keep") {
        return;
      }
      const changed = nb !== S.bob;
      S.bob = nb;
      this.startRound(changed ? "bobcall" : "eyes1");
    },
    endGame(t) {
      const S = this.S;
      if (t !== S.host) return;
      S.phase = "over";
      S.timer.endsAt = null;
      this.broadcast();
    },
  };

  addPlayer(t) {
    const S = this.S;
    if (Object.values(S.players).filter(p => !p.left).length >= MAX_PLAYERS) return "This game is full.";
    if (S.players[t]) S.players[t].left = false;
    else S.players[t] = { num: S.nextNum++, left: false };
    return null;
  }
}
