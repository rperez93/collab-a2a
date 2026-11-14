/* Separate first-key ceremony. Only the hub registers a key or verifies proof. */
import {encodeBinary, creationOptions, requestOptions,
  serializeCredential, readJSON} from "./passkey.js";

export function createdKey(credential) {
  if (!credential || credential.type !== "public-key" || !credential.response ||
      typeof credential.response.getPublicKey !== "function" ||
      typeof credential.response.getPublicKeyAlgorithm !== "function") {
    throw new Error("This browser cannot export the required public key. No fallback was selected.");
  }
  const id = encodeBinary(credential.rawId, 1024, "created credential ID");
  if (credential.id !== id) throw new Error("The created credential ID is not canonical.");
  const algorithm = credential.response.getPublicKeyAlgorithm();
  if (algorithm !== -7 && algorithm !== -257) throw new Error("The key algorithm is unsupported.");
  return {credential_id: id, algorithm,
    public_key_spki: encodeBinary(credential.response.getPublicKey(), 4096, "public key")};
}

export function possessionOptions(options, key, remaining) {
  if (!key || !Number.isFinite(remaining) || remaining < 1) throw new Error("The ceremony expired.");
  const publicKey = requestOptions({challenge: options.publicKey.challenge, rpId: "localhost",
    allowCredentials: [{type: "public-key", id: key.credential_id}], timeout: 120000});
  publicKey.timeout = Math.min(publicKey.timeout, Math.floor(remaining));
  if (publicKey.allowCredentials.length !== 1 ||
      encodeBinary(publicKey.allowCredentials[0].id, 1024) !== key.credential_id) {
    throw new Error("The possession step selected another credential.");
  }
  return publicKey;
}

export function createEnrolApp(environment = globalThis) {
  const {document, location, history, navigator, fetch, AbortController} = environment;
  const elements = Object.fromEntries(["status", "session", "participant-id", "participant-name", "trust",
    "credential", "usable-at", "create", "prove", "refresh", "cancel"].map(id => [id, document.getElementById(id)]));
  const path = /^\/enrol\/([A-Za-z0-9_-]{1,128})\/?$/.exec(location.pathname);
  const token = location.hash.slice(1);
  history.replaceState(null, "", location.pathname + location.search);
  let identity = null, options = null, key = null, phase = "unknown", active = null;
  let sequence = 0, disposed = false, uncertain = false;
  const now = () => environment.Date.now();
  const monotonic = () => environment.performance.now();
  function status(text, error = false) { elements.status.textContent = text; elements.status.dataset.kind = error ? "error" : "info"; }
  function buttons() {
    const idle = !disposed && !active && !uncertain;
    elements.create.disabled = !idle || !identity || phase !== "ready";
    elements.prove.disabled = !idle || !key || !options || phase !== "ceremony" || now() >= options.expires_at * 1000;
    elements.refresh.disabled = disposed || Boolean(active);
    elements.cancel.disabled = !active;
  }
  function stop(message) {
    sequence += 1;
    if (active?.forwardingStarted) uncertain = true;
    if (active) { active.controller.abort(); environment.clearTimeout(active.timer); }
    active = null;
    buttons();
    if (message) status(uncertain ? "Enrolment status is unknown. Refresh hub status; do not repeat the finish." :
      "Cancelled locally. A credential may remain on the authenticator; hub enrolment was not confirmed.", true);
  }
  function current(operation) {
    if (disposed || active !== operation || operation.sequence !== sequence || monotonic() >= operation.deadline) {
      throw new Error("This enrolment step expired or is no longer current.");
    }
  }
  function ceremonyDeadline(operation, expires) {
    const remaining = Math.min(operation.deadline - monotonic(), expires * 1000 - now());
    if (!Number.isFinite(remaining) || remaining < 1) throw new Error("The ceremony expired.");
    operation.deadline = monotonic() + remaining;
    environment.clearTimeout(operation.timer);
    operation.timer = environment.setTimeout(() => stop(true), remaining);
    return remaining;
  }
  async function rpc(action, operation, body) {
    current(operation);
    if (action === "finish") operation.forwardingStarted = true;
    const response = await fetch(`/enrol/${path[1]}/${action}`, {method: body === undefined ? "GET" : "POST",
      mode: "same-origin", credentials: "omit", redirect: "error", cache: "no-store", referrerPolicy: "no-referrer",
      headers: {"X-Collab-Approval-Token": token, ...(body === undefined ? {} : {"Content-Type": "application/json"})},
      body: body === undefined ? undefined : JSON.stringify(body), signal: operation.controller.signal});
    const value = await readJSON(response);
    current(operation);
    return value;
  }
  async function run(action) {
    if (active || disposed) return;
    let operation;
    try {
      if (!path || location.hostname !== "localhost" || location.search || environment.isSecureContext === false ||
          !/^[A-Za-z0-9_-]{43}$/.test(token)) throw new Error("Open the exact human-started localhost enrolment link.");
      operation = {sequence: ++sequence, controller: new AbortController(), deadline: monotonic() + 120000,
        forwardingStarted: false, timer: null};
      operation.timer = environment.setTimeout(() => stop(true), 120000);
      active = operation; buttons();
      await action(operation);
    } catch (error) {
      if (!operation || active === operation) {
        if (operation?.forwardingStarted) uncertain = true;
        const message = uncertain ? "Enrolment status is unknown. Refresh hub status; do not repeat the finish." :
          "Enrolment was not confirmed. A created credential may remain on the authenticator. " +
          (typeof error?.message === "string" && error.message.length <= 2048 ? error.message : "Refresh hub status.");
        status(message, true);
      }
    } finally {
      if (active === operation) { environment.clearTimeout(operation.timer); active = null; buttons(); }
    }
  }
  function show(data) {
    if (!data || typeof data.session_id !== "string" || data.session_id.length > 128 ||
        typeof data.participant_id !== "string" || !/^p_[A-Za-z0-9_-]{1,126}$/.test(data.participant_id) ||
        new TextEncoder().encode(data.participant_id).length > 64 ||
        typeof data.display_name !== "string" || new TextEncoder().encode(data.display_name).length > 256 ||
        data.trust !== "tofu-unverified" ||
        !["ready", "ceremony", "enrolled", "exists", "uncertain", "begin-uncertain"].includes(data.status)) {
      throw new Error("The enrolment identity or state is invalid.");
    }
    if (identity && (identity.session_id !== data.session_id || identity.participant_id !== data.participant_id)) {
      throw new Error("The enrolment principal changed.");
    }
    identity = data;
    phase = data.status;
    uncertain = phase === "uncertain" || phase === "begin-uncertain";
    elements.session.textContent = data.session_id;
    elements["participant-id"].textContent = data.participant_id;
    elements["participant-name"].textContent = data.display_name;
    elements.trust.textContent = "Unverified first-key enrolment (TOFU); no hardware attestation established.";
    if (phase === "enrolled") {
      const metadata = data.credential;
      if (!metadata || typeof metadata.credential_id !== "string" || metadata.credential_id.length > 1366 ||
          !Number.isFinite(metadata.usable_at) || !/^[a-f0-9]{64}$/.test(metadata.fingerprint) ||
          !["active", "frozen", "retired"].includes(metadata.state)) {
        throw new Error("The hub key confirmation is invalid.");
      }
      elements.credential.textContent = `${metadata.state}: ${metadata.fingerprint}`;
      elements["usable-at"].textContent = new environment.Date(metadata.usable_at * 1000).toISOString();
      key = null; options = null;
    }
    status(phase === "ready" ? "Check the participant identity, then create your first passkey." :
      phase === "ceremony" ? key ? "Creation completed locally. Prove possession with the same credential." :
        "A ceremony is already reserved. Its key is not available in this page; enrolment is not confirmed." :
      phase === "enrolled" ? data.credential.state === "active" ?
        "The hub registered the key. Its reported cooldown still applies; no operation was approved." :
        `The hub reports this key as ${data.credential.state}. It cannot approve operations; use the separate signed key-change or recovery flow.` :
      phase === "exists" ? "A key already exists. Use the separate signed key-change or recovery flow." :
      "Enrolment is unconfirmed. Refresh status; do not start another ceremony.", uncertain);
  }
  async function load() { return run(async operation => show(await rpc("status", operation))); }
  async function create() {
    return run(async operation => {
      if (!identity || phase !== "ready" || uncertain) throw new Error("A current confirmed identity is required.");
      if (!navigator.credentials || typeof navigator.credentials.create !== "function" ||
          typeof navigator.credentials.get !== "function") throw new Error("Passkey creation and assertion are required.");
      options = await rpc("options", operation, {});
      phase = "ceremony";
      if (typeof options.challenge_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(options.challenge_id) ||
          !Number.isFinite(options.expires_at) || options.expires_at * 1000 <= now() ||
          options.expires_at * 1000 - now() > 120000) throw new Error("The ceremony is invalid or expired.");
      const remaining = ceremonyDeadline(operation, options.expires_at);
      const publicKey = creationOptions(options.publicKey, identity.participant_id);
      const allowed = options.publicKey.pubKeyCredParams;
      if (!Array.isArray(allowed) || !allowed.length || allowed.some(item => item.type !== "public-key" || ![-7, -257].includes(item.alg))) {
        throw new Error("The hub supplied unsupported algorithms.");
      }
      publicKey.pubKeyCredParams = publicKey.pubKeyCredParams.filter(item => allowed.some(value => value.alg === item.alg));
      publicKey.timeout = Math.min(publicKey.timeout, Math.floor(remaining));
      current(operation);
      const credential = await navigator.credentials.create({publicKey, signal: operation.controller.signal});
      current(operation);
      if (now() >= options.expires_at * 1000) throw new Error("The creation ceremony expired.");
      const created = createdKey(credential);
      if (!allowed.some(item => item.alg === created.algorithm)) throw new Error("The created key algorithm was not offered.");
      key = created;
      status("Credential created locally. Use the second button to prove possession and enrol with the hub.");
    });
  }
  async function prove() {
    return run(async operation => {
      if (!identity || phase !== "ceremony" || !key || !options || uncertain) throw new Error("The created key is unavailable.");
      const remaining = ceremonyDeadline(operation, options.expires_at);
      const publicKey = possessionOptions(options, key, remaining);
      const assertion = await navigator.credentials.get({publicKey, signal: operation.controller.signal});
      current(operation);
      if (now() >= options.expires_at * 1000) throw new Error("The possession ceremony expired.");
      const credential = serializeCredential(assertion, false);
      if (credential.id !== key.credential_id) throw new Error("The possession credential differs from the created key.");
      const value = await rpc("finish", operation, {challenge_id: options.challenge_id, created_key: key, credential});
      if (value.status !== "enrolled") throw new Error("The hub did not confirm enrolment.");
      // Reconcile through the authoritative key registry before displaying it.
      show(await rpc("status", operation));
    });
  }
  const app = {load, create, prove, cancel: () => stop(true),
    dispose: () => { disposed = true; stop(false); }};
  for (const name of ["create", "prove", "refresh", "cancel"]) {
    elements[name].addEventListener("click", () => app[name === "refresh" ? "load" : name]());
  }
  buttons();
  return app;
}

if (typeof window !== "undefined" && typeof document !== "undefined" && /^\/enrol\/[A-Za-z0-9_-]{1,128}\/?$/.test(window.location.pathname)) {
  const app = createEnrolApp(window);
  window.addEventListener("pagehide", () => app.dispose(), {once: true});
  void app.load();
}
