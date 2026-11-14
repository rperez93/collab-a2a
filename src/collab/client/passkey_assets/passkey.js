/* This local page forwards evidence; only the hub verifies signatures and trust. */
export const LIMITS = Object.freeze({rpc: 512 * 1024, review: 64 * 1024, challenge: 32,
  credential: 1024, clientData: 16384, authenticator: 8192, signature: 8192,
  attestation: 65536, publicKey: 8192, credentials: 32});
const utf8 = new TextEncoder();

function boundedText(value, maximum, label) {
  if (typeof value !== "string" || utf8.encode(value).length > maximum) {
    throw new Error(`Invalid or oversized ${label}.`);
  }
  return value;
}

export function encodeBinary(value, maximum, label = "binary field") {
  const bytes = value instanceof ArrayBuffer ? new Uint8Array(value) :
    ArrayBuffer.isView(value) ? new Uint8Array(value.buffer, value.byteOffset, value.byteLength) : null;
  if (!bytes || !bytes.length || bytes.length > maximum) throw new Error(`Invalid ${label}.`);
  let text = "";
  for (let offset = 0; offset < bytes.length; offset += 4096) {
    text += String.fromCharCode(...bytes.subarray(offset, offset + 4096));
  }
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function decodeBinary(value, maximum, label = "binary field") {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value) ||
      value.length > Math.ceil(maximum * 4 / 3) || value.length % 4 === 1) {
    throw new Error(`Invalid ${label}.`);
  }
  let decoded;
  try { decoded = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4)); }
  catch { throw new Error(`Invalid ${label}.`); }
  const bytes = Uint8Array.from(decoded, character => character.charCodeAt(0));
  if (!bytes.length || bytes.length > maximum || encodeBinary(bytes, maximum) !== value) {
    throw new Error(`Invalid ${label}.`);
  }
  return bytes.buffer;
}

function challenge(value) {
  const decoded = decodeBinary(value, LIMITS.challenge, "challenge");
  if (decoded.byteLength !== 32) throw new Error("The challenge must contain exactly 32 bytes.");
  return decoded;
}

function timeout(value) {
  if (value === undefined) return 120000;
  if (!Number.isFinite(value) || value < 1000 || value > 120000) throw new Error("Invalid ceremony timeout.");
  return value;
}

function descriptors(values, required = false) {
  if (values === undefined && !required) return [];
  if (!Array.isArray(values) || values.length > LIMITS.credentials || (required && !values.length)) {
    throw new Error("Invalid credential list.");
  }
  return values.map(value => {
    if (!value || value.type !== "public-key") throw new Error("Unsupported credential type.");
    const result = {type: "public-key", id: decodeBinary(value.id, LIMITS.credential, "credential ID")};
    if (value.transports !== undefined) {
      const allowed = new Set(["usb", "nfc", "ble", "internal", "hybrid"]);
      if (!Array.isArray(value.transports) || value.transports.length > 5 ||
          value.transports.some(item => !allowed.has(item))) throw new Error("Invalid credential transport.");
      result.transports = [...value.transports];
    }
    return result;
  });
}

export function creationOptions(value) {
  if (!value || !value.rp || value.rp.id !== "localhost" || !value.user) {
    throw new Error("Enrolment must use the localhost relying party.");
  }
  return {challenge: challenge(value.challenge),
    rp: {id: "localhost", name: boundedText(value.rp.name, 256, "relying party name")},
    user: {id: decodeBinary(value.user.id, 64, "user ID"),
      name: boundedText(value.user.name, 256, "user name"),
      displayName: boundedText(value.user.displayName, 256, "display name")},
    pubKeyCredParams: [{type: "public-key", alg: -7}, {type: "public-key", alg: -257}],
    authenticatorSelection: {userVerification: "required"},
    attestation: "direct", timeout: timeout(value.timeout),
    excludeCredentials: descriptors(value.excludeCredentials)};
}

export function requestOptions(value) {
  if (!value || value.rpId !== "localhost") throw new Error("Approval must use the localhost relying party.");
  return {challenge: challenge(value.challenge), rpId: "localhost",
    allowCredentials: descriptors(value.allowCredentials, true), userVerification: "required",
    timeout: timeout(value.timeout)};
}

export function serializeCredential(credential, enrolment) {
  if (!credential || credential.type !== "public-key" || !credential.response) {
    throw new Error("The browser returned no public-key credential.");
  }
  const rawId = encodeBinary(credential.rawId, LIMITS.credential, "raw credential ID");
  if (credential.id !== rawId) throw new Error("The credential ID does not match its raw bytes.");
  const response = credential.response;
  const evidence = {clientDataJSON: encodeBinary(response.clientDataJSON, LIMITS.clientData, "client data")};
  if (enrolment) {
    if (typeof response.getPublicKey !== "function" || typeof response.getPublicKeyAlgorithm !== "function") {
      throw new Error("This browser cannot export the enrolment public key. No weaker fallback was selected.");
    }
    const algorithm = response.getPublicKeyAlgorithm();
    if (algorithm !== -7 && algorithm !== -257) throw new Error("The browser returned an unsupported public-key algorithm.");
    evidence.publicKey = encodeBinary(response.getPublicKey(), LIMITS.publicKey, "SPKI public key");
    evidence.publicKeyAlgorithm = algorithm;
    evidence.attestationObject = encodeBinary(response.attestationObject, LIMITS.attestation, "attestation object");
    if (typeof response.getAuthenticatorData === "function") {
      evidence.authenticatorData = encodeBinary(response.getAuthenticatorData(), LIMITS.authenticator, "authenticator data");
    }
  } else {
    evidence.authenticatorData = encodeBinary(response.authenticatorData, LIMITS.authenticator, "authenticator data");
    evidence.signature = encodeBinary(response.signature, LIMITS.signature, "signature");
  }
  return {id: credential.id, rawId, type: "public-key", response: evidence};
}

export async function readJSON(response) {
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    throw new Error("The local adapter did not return JSON.");
  }
  const declared = response.headers.get("content-length");
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > LIMITS.rpc)) {
    throw new Error("The local response exceeds its limit.");
  }
  if (!response.body?.getReader) throw new Error("Bounded response streaming is unavailable.");
  const reader = response.body.getReader();
  const parts = [];
  let size = 0;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > LIMITS.rpc) throw new Error("The local response exceeds its limit.");
      parts.push(value);
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  const joined = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { joined.set(part, offset); offset += part.byteLength; }
  let result;
  try { result = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(joined)); }
  catch { throw new Error("The local adapter returned malformed JSON."); }
  if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("Invalid local response.");
  if (!response.ok) throw new Error(boundedText(result.message || "The local adapter refused this operation.", 2048, "error message"));
  return result;
}

export function createApp(environment = globalThis) {
  const {document, location, history, navigator, fetch, AbortController} = environment;
  const elements = Object.fromEntries(["status", "subject", "requester", "destination", "authority", "digest",
    "expires", "trust", "trust-detail", "review", "approve", "decline", "cancel", "refresh", "enrol"]
    .map(name => [name, document.getElementById(name)]));
  const path = /^\/consent\/([A-Za-z0-9_-]{1,128})\/?$/.exec(location.pathname);
  const token = location.hash.slice(1);
  // Drop the capability before any request. It is never persisted or copied to a query.
  history.replaceState(null, "", location.pathname + location.search);
  let review = null, active = null, sequence = 0, disposed = false, done = false, uncertain = false, expiryTimer = null;
  const now = () => environment.Date.now();
  const monotonic = () => environment.performance.now();
  function status(text, error = false) { elements.status.textContent = text; elements.status.dataset.kind = error ? "error" : "info"; }
  function expired() { return !review || now() >= review.summary.expires * 1000; }
  function buttons() {
    const ready = Boolean(review) && !expired() && !done && !uncertain && !disposed;
    elements.approve.disabled = !ready || Boolean(active) || !review.enrolled;
    elements.enrol.disabled = !ready || Boolean(active) || review.enrolled;
    elements.decline.disabled = !ready || Boolean(active);
    elements.refresh.disabled = Boolean(active) || disposed;
    elements.cancel.disabled = !active;
  }
  function stop(message) {
    sequence += 1;
    if (active?.forwardingStarted) uncertain = true;
    if (message && uncertain) message = "Status unknown. Check status by refreshing the review before taking another action.";
    if (active) { active.controller.abort(); environment.clearTimeout(active.timer); }
    active = null; buttons();
    if (message) status(message, true);
  }
  function start() {
    if (active || disposed) throw new Error("Another operation already owns this request.");
    const operation = {id: ++sequence, controller: new AbortController(), deadline: monotonic() + 120000, forwardingStarted: false};
    operation.timer = environment.setTimeout(() => stop("The operation timed out. Refresh the review; no weaker fallback was selected."), 120000);
    active = operation; buttons();
    return operation;
  }
  function current(operation) {
    if (disposed || active !== operation || operation.id !== sequence || monotonic() >= operation.deadline) {
      throw new Error("The local operation is no longer current.");
    }
    if (review && expired()) throw new Error("This review has expired. Refresh it before deciding.");
  }
  async function rpc(suffix, operation, body) {
    current(operation);
    if (suffix.endsWith("/result") || suffix === "decline") operation.forwardingStarted = true;
    const response = await fetch(`/consent/${path[1]}/${suffix}`, {method: body === undefined ? "GET" : "POST",
      mode: "same-origin", credentials: "omit", redirect: "error", referrerPolicy: "no-referrer", cache: "no-store",
      headers: {"X-Collab-Approval-Token": token, ...(body === undefined ? {} : {"Content-Type": "application/json"})},
      body: body === undefined ? undefined : JSON.stringify(body), signal: operation.controller.signal});
    const result = await readJSON(response);
    current(operation);
    return result;
  }
  async function run(action) {
    let operation;
    try {
      if (!path || location.hostname !== "localhost" || environment.isSecureContext === false ||
          !/^[A-Za-z0-9_-]{43}$/.test(token) || location.search) {
        throw new Error("Open the exact localhost approval link supplied for this request. Its capability must be in the fragment.");
      }
      operation = start();
      await action(operation);
    } catch (error) {
      if (!operation || active === operation) {
        if (operation?.forwardingStarted) uncertain = true;
        const cancelled = error?.name === "NotAllowedError" || error?.name === "AbortError";
        status(uncertain ? "Status unknown. Check status by refreshing the review before taking another action." : cancelled ? "Cancelled or timed out. Refresh the review; no weaker fallback was selected." :
          boundedText(error?.message || "Operation not confirmed. Refresh the review before trying again.", 2048, "error"), true);
      }
    } finally {
      if (active === operation) { environment.clearTimeout(operation.timer); active = null; buttons(); }
    }
  }
  async function load() {
    return run(async operation => {
      // A refresh must be able to replace an expired review using the same in-memory capability.
      if (expiryTimer) environment.clearTimeout(expiryTimer);
      review = null;
      const data = await rpc("review", operation);
      if (data.request_id !== path[1] || !data.summary || !data.trust || typeof data.enrolled !== "boolean") {
        throw new Error("The review does not belong to this request.");
      }
      boundedText(data.canonical_review, LIMITS.review, "canonical review");
      if (!Number.isFinite(data.summary.expires) || data.summary.expires <= 0) throw new Error("Invalid review expiry.");
      for (const name of ["subject", "requester", "destination", "authority", "digest"]) {
        elements[name].textContent = boundedText(data.summary[name], 8192, name);
      }
      elements.review.textContent = data.canonical_review;
      elements.expires.textContent = new environment.Date(data.summary.expires * 1000).toISOString();
      elements.trust.textContent = boundedText(data.trust.label, 256, "reported trust");
      elements["trust-detail"].textContent = boundedText(data.trust.detail, 8192, "trust detail");
      const state = data.status ?? (uncertain ? "unknown" : "pending");
      if (!["pending", "unknown", "uncertain", "busy", "approved", "declined", "refused", "expired"].includes(state)) {
        throw new Error("The adapter returned an unknown request status.");
      }
      review = data;
      uncertain = ["unknown", "uncertain", "busy"].includes(state);
      done = ["approved", "declined", "refused", "expired"].includes(state);
      if (expiryTimer) environment.clearTimeout(expiryTimer);
      expiryTimer = environment.setTimeout(() => { stop("This review has expired. Refresh it before deciding."); buttons(); },
        Math.min(2147483647, Math.max(0, data.summary.expires * 1000 - now())));
      status(uncertain ? "Status unknown. Check status again before taking another action." :
        done ? `Hub-reported request status: ${state}.` :
        expired() ? "This review has expired. Refresh it before deciding." :
        "Read the complete untrusted review, then choose a decision.", uncertain || expired());
    });
  }
  function confirm(result, expected) {
    const message = boundedText(result.message || "", 2048, "result");
    if (result.status === expected) {
      uncertain = false;
      status(`Hub response: ${result.status}. ${message}`);
      return true;
    }
    if (["pending", "unknown", "uncertain", "busy"].includes(result.status)) {
      uncertain = true;
      status("Status unknown. Check status by refreshing the review before taking another action.", true);
      return false;
    }
    if (["refused", "expired"].includes(result.status)) {
      uncertain = false; done = true;
      if (expiryTimer) environment.clearTimeout(expiryTimer);
      status(`Hub response: ${result.status}. ${message}`, true);
      return false;
    }
    throw new Error("The hub did not confirm the requested operation.");
  }
  async function ceremony(enrolment) {
    return run(async operation => {
      if (!review || expired() || done || uncertain) throw new Error("A current confirmed review is required before deciding; refresh to check status.");
      if (!navigator.credentials || typeof navigator.credentials[enrolment ? "create" : "get"] !== "function") {
        throw new Error("This browser has no supported passkey API. No weaker fallback was selected.");
      }
      const prefix = enrolment ? "enrol" : "approve";
      const options = await rpc(`${prefix}/options`, operation, {});
      boundedText(options.ceremony_id, 256, "ceremony ID");
      if (!options.ceremony_id) throw new Error("Missing ceremony ID.");
      const publicKey = enrolment ? creationOptions(options.publicKey) : requestOptions(options.publicKey);
      if (!Number.isFinite(options.expires)) throw new Error("Missing ceremony expiry.");
      const remaining = Math.min(120000, operation.deadline - monotonic(),
        options.expires * 1000 - now(), review.summary.expires * 1000 - now());
      if (remaining < 1) throw new Error("The ceremony has expired. Refresh the review.");
      operation.deadline = monotonic() + remaining;
      environment.clearTimeout(operation.timer);
      operation.timer = environment.setTimeout(() => stop("The ceremony expired. Refresh the review."), remaining);
      publicKey.timeout = Math.min(publicKey.timeout, Math.floor(remaining));
      current(operation);
      const credential = await navigator.credentials[enrolment ? "create" : "get"]({publicKey, signal: operation.controller.signal});
      current(operation);
      const evidence = serializeCredential(credential, enrolment);
      const result = await rpc(`${prefix}/result`, operation, {ceremony_id: options.ceremony_id, credential: evidence});
      if (!confirm(result, enrolment ? "enrolled" : "approved")) return;
      if (enrolment) review.enrolled = true;
      else { done = true; if (expiryTimer) environment.clearTimeout(expiryTimer); }
    });
  }
  function decline() {
    return run(async operation => {
      if (!review || expired() || done || uncertain) throw new Error("A current confirmed review is required before declining; refresh to check status.");
      const result = await rpc("decline", operation, {});
      if (!confirm(result, "declined")) return;
      done = true; if (expiryTimer) environment.clearTimeout(expiryTimer);
    });
  }
  const app = {load, enrol: () => ceremony(true), approve: () => ceremony(false), decline,
    cancel: () => stop("Cancelled locally. Refresh the review to check its status before trying again."),
    dispose: () => { disposed = true; stop(); if (expiryTimer) environment.clearTimeout(expiryTimer); }};
  for (const name of ["approve", "enrol", "decline", "refresh", "cancel"]) {
    elements[name].addEventListener("click", () => app[name === "refresh" ? "load" : name]());
  }
  buttons();
  return app;
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  try {
    const app = createApp(window);
    window.addEventListener("pagehide", () => app.dispose(), {once: true});
    void app.load();
  } catch {
    const status = document.getElementById("status");
    if (status) {
      status.textContent = "This browser could not initialize the local approval page. No fallback was selected.";
      status.dataset.kind = "error";
    }
  }
}
