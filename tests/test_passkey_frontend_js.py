"""Browser contract controls run the actual JS in Node, never WebAuthn or a real browser."""
from pathlib import Path
import shutil
import subprocess

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "src/collab/client/passkey_assets"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node is unavailable for frontend contract checks")

HARNESS = r'''
import assert from 'node:assert/strict';
import * as page from './passkey.mjs';
const bytes = (...values) => Uint8Array.from(values).buffer;
const b64 = value => page.encodeBinary(value, 65536);
const credential = (enrol = false) => ({id: b64(bytes(0,255,2)), rawId: bytes(0,255,2), type:'public-key',
 response: enrol ? {clientDataJSON:bytes(1,2,3),attestationObject:bytes(4,5),getPublicKey:()=>bytes(6,7),
  getPublicKeyAlgorithm:()=>-7,getAuthenticatorData:()=>bytes(8,9)} :
 {clientDataJSON:bytes(1,2,3),authenticatorData:bytes(8,9),signature:bytes(10,11)}});
const publicKey = {challenge:b64(new Uint8Array(32).fill(1)),rpId:'localhost',rp:{id:'localhost',name:'Collab'},
 user:{id:b64(bytes(4)),name:'alice',displayName:'Alice'},
 allowCredentials:[{type:'public-key',id:b64(bytes(0,255,2))}],userVerification:'discouraged'};
function setup({enrolled=true, credentials={}, overrides={}}={}) {
 let wall=1700000000000; let mono=0; let timerId=0;
 const timers=new Map(), elements={}, calls=[], prompts=[];
 const ids=['status','subject','requester','destination','authority','digest','expires','trust','trust-detail',
 'review','approve','decline','cancel','refresh','enrol'];
 for (const id of ids) {
  elements[id]={textContent:'',dataset:{},disabled:false,handlers:{},addEventListener(name,handler){this.handlers[name]=handler;}};
  Object.defineProperty(elements[id],'innerHTML',{set(){assert.fail('untrusted HTML sink used');}});
 }
 const review={request_id:'r_test',status:'pending',canonical_review:'<img src=x onerror=alert(1)>\n☃ full canonical review',
 summary:{subject:'untrusted <script>',requester:'peer',destination:'this agent',authority:'run requested work',
 digest:'a'.repeat(64),expires:wall/1000+60},trust:{label:'TOFU, not attested',detail:'Hub reported; not locally verified.'},enrolled};
 const env={isSecureContext:true,document:{getElementById:id=>elements[id]},location:{pathname:'/consent/r_test',hash:'#'+'A'.repeat(43),
 hostname:'localhost',search:''},history:{replaceState(...args){calls.push(['strip',args]);}},
 navigator:{credentials:{create:async value=>{prompts.push(['create',value]);return credential(true);},
 get:async value=>{prompts.push(['get',value]);return credential(false);},...credentials}},
 AbortController, Date:class extends Date{static now(){return wall;}},performance:{now:()=>mono},
 setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},
 fetch:async(url,options)=>{
  calls.push(['fetch',url,options]);
  const suffix=url.slice('/consent/r_test/'.length);
  let body=suffix==='review'?review:suffix.endsWith('/options')?{ceremony_id:'c_test',publicKey,expires:wall/1000+30}:
   {status:suffix==='enrol/result'?'enrolled':suffix==='decline'?'declined':'approved',message:'Reported by hub.'};
  if (overrides[suffix]) return overrides[suffix](options);
  return new Response(JSON.stringify(body),{headers:{'content-type':'application/json'}});
 }};
 const app=page.createApp(env);
 return {app,env,review,elements,calls,prompts,timers,advance(ms){wall+=ms;mono+=ms;}};
}
const flush = () => new Promise(resolve=>setImmediate(resolve));
'''


def run_js(tmp_path, body):
    """Use a fresh Node process with copied assets and no network/provider calls."""
    shutil.copyfile(ASSETS / "passkey.js", tmp_path / "passkey.mjs")
    script = tmp_path / "contract.mjs"
    script.write_text(HARNESS + "\n" + body)
    done = subprocess.run([NODE, str(script)], cwd=tmp_path, capture_output=True,
                          text=True, timeout=8)
    assert done.returncode == 0, done.stdout + done.stderr


def test_binary_fields_are_canonical_bounded_and_byte_exact(tmp_path):
    run_js(tmp_path, r'''
const original=bytes(0,255,128,1,2);
assert.deepEqual(new Uint8Array(page.decodeBinary(b64(original),64)),new Uint8Array(original));
for (const value of ['','A','AB','YQ==',' YQ','YQ\n','*']) assert.throws(()=>page.decodeBinary(value,64));
assert.throws(()=>page.decodeBinary('YWFh',2));
assert.throws(()=>page.encodeBinary(bytes(),64));
assert.throws(()=>page.encodeBinary(bytes(1,2,3),2));
''')


def test_options_pin_localhost_and_require_user_verification_and_supported_algorithms(tmp_path):
    run_js(tmp_path, r'''
const created=page.creationOptions(publicKey), requested=page.requestOptions(publicKey);
assert.equal(created.rp.id,'localhost');assert.equal(created.authenticatorSelection.userVerification,'required');
assert.deepEqual(created.pubKeyCredParams.map(x=>x.alg),[-7,-257]);
assert.equal(requested.rpId,'localhost');assert.equal(requested.userVerification,'required');
assert.deepEqual(new Uint8Array(requested.allowCredentials[0].id),new Uint8Array(bytes(0,255,2)));
assert.throws(()=>page.creationOptions({...publicKey,rp:{id:'evil.example',name:'evil'}}));
assert.throws(()=>page.requestOptions({...publicKey,rpId:'127.0.0.1'}));
assert.throws(()=>page.requestOptions({...publicKey,allowCredentials:[]}));
assert.throws(()=>page.requestOptions({...publicKey,timeout:Infinity}));
''')


def test_registration_exports_spki_and_preserves_evidence_without_claiming_attestation(tmp_path):
    run_js(tmp_path, r'''
const raw=credential(true), output=page.serializeCredential(raw,true);
assert.equal(output.rawId,raw.id);assert.equal(output.response.clientDataJSON,b64(bytes(1,2,3)));
assert.equal(output.response.publicKey,b64(bytes(6,7)));assert.equal(output.response.publicKeyAlgorithm,-7);
assert.equal(output.response.attestationObject,b64(bytes(4,5)));
assert.equal(output.response.authenticatorData,b64(bytes(8,9)));
assert.equal(output.attested,undefined);assert.equal(output.strong,undefined);
raw.response.getPublicKeyAlgorithm=()=>-257;assert.equal(page.serializeCredential(raw,true).response.publicKeyAlgorithm,-257);
raw.response.getPublicKey=()=>null;assert.throws(()=>page.serializeCredential(raw,true));
raw.response.getPublicKey=()=>bytes(6);raw.response.getPublicKeyAlgorithm=()=>-8;
assert.throws(()=>page.serializeCredential(raw,true));
delete raw.response.getPublicKey;assert.throws(()=>page.serializeCredential(raw,true));
''')


def test_response_json_is_bounded_before_parse_and_refuses_malformed_records(tmp_path):
    run_js(tmp_path, r'''
let reads=0;
await assert.rejects(page.readJSON({ok:true,headers:new Headers({'content-type':'application/json','content-length':String(page.LIMITS.rpc+1)}),body:{getReader(){reads++;}}}));
assert.equal(reads,0);
await assert.rejects(page.readJSON(new Response('{',{headers:{'content-type':'application/json'}})));
await assert.rejects(page.readJSON(new Response('[]',{headers:{'content-type':'application/json'}})));
await assert.rejects(page.readJSON(new Response('x'.repeat(page.LIMITS.rpc+1),{headers:{'content-type':'application/json'}})));
''')


def test_loading_strips_the_capability_and_renders_untrusted_review_only_as_text(tmp_path):
    run_js(tmp_path, r'''
const view=setup();await view.app.load();
assert.equal(view.calls[0][0],'strip');assert.equal(view.calls[0][1][2],'/consent/r_test');
assert.equal(view.prompts.length,0);assert.equal(view.elements.review.textContent,view.review.canonical_review);
assert.equal(view.elements.subject.textContent,'untrusted <script>');
const request=view.calls.find(x=>x[0]==='fetch');
assert.equal(request[1],'/consent/r_test/review');assert.equal(request[2].headers['X-Collab-Approval-Token'],'A'.repeat(43));
assert.equal(request[2].credentials,'omit');assert.equal(request[2].redirect,'error');
view.app.dispose();
''')


def test_only_an_explicit_approval_forwards_the_original_assertion_bytes(tmp_path):
    run_js(tmp_path, r'''
const view=setup();await view.app.load();assert.equal(view.prompts.length,0);
await view.elements.approve.handlers.click();await flush();
assert.equal(view.prompts.length,1);assert.equal(view.prompts[0][0],'get');
assert.equal(view.prompts[0][1].publicKey.userVerification,'required');
const posted=view.calls.find(x=>x[1]==='/consent/r_test/approve/result');
const body=JSON.parse(posted[2].body);
assert.equal(body.ceremony_id,'c_test');assert.equal(body.credential.response.signature,b64(bytes(10,11)));
assert.equal(body.credential.response.authenticatorData,b64(bytes(8,9)));
assert.equal(body.credential.response.clientDataJSON,b64(bytes(1,2,3)));
assert.match(view.elements.status.textContent,/Hub response: approved/);assert.equal(view.elements.approve.disabled,true);
view.app.dispose();
''')


def test_unsupported_registration_and_user_cancellation_never_select_a_weak_fallback(tmp_path):
    run_js(tmp_path, r'''
const unsupported=setup({enrolled:false,credentials:{create:async()=>{const c=credential(true);delete c.response.getPublicKey;return c;}}});
await unsupported.app.load();await unsupported.app.enrol();
assert.match(unsupported.elements.status.textContent,/cannot export/);
assert.equal(unsupported.calls.filter(x=>x[0]==='fetch' && x[1].endsWith('/result')).length,0);
unsupported.app.dispose();
const cancelled=setup({credentials:{get:async()=>{throw new DOMException('denied','NotAllowedError');}}});
await cancelled.app.load();await cancelled.app.approve();
assert.match(cancelled.elements.status.textContent,/Cancelled or timed out/);
assert.equal(cancelled.calls.filter(x=>x[0]==='fetch' && x[1].endsWith('/result')).length,0);cancelled.app.dispose();
''')


def test_cancel_fences_late_browser_results_before_any_result_post(tmp_path):
    run_js(tmp_path, r'''
let release;const result=new Promise(resolve=>release=resolve);
const view=setup({credentials:{get:()=>result}});await view.app.load();
const pending=view.app.approve();await flush();
view.app.cancel();release(credential());await pending;
assert.equal(view.calls.filter(x=>x[0]==='fetch' && x[1].endsWith('/result')).length,0);
assert.match(view.elements.status.textContent,/Cancelled locally/);view.app.dispose();
''')


def test_expiry_fences_late_browser_results_and_decline_never_prompts(tmp_path):
    run_js(tmp_path, r'''
let release;const result=new Promise(resolve=>release=resolve);
const view=setup({credentials:{get:()=>result}});await view.app.load();
const pending=view.app.approve();await flush();view.advance(61000);
const expiry=[...view.timers.values()].find(x=>x.delay===60000);assert.ok(expiry);expiry.fn();
release(credential());await pending;
assert.equal(view.calls.filter(x=>x[0]==='fetch' && x[1].endsWith('/result')).length,0);
assert.match(view.elements.status.textContent,/expired/);view.app.dispose();
const declined=setup();await declined.app.load();await declined.app.decline();
assert.equal(declined.prompts.length,0);assert.match(declined.elements.status.textContent,/Hub response: declined/);
declined.app.dispose();
''')


def test_a_missing_fragment_capability_or_wrong_origin_never_fetches_or_prompts(tmp_path):
    run_js(tmp_path, r'''
for (const change of ['missing','origin','query']) {
 const view=setup();
 if(change==='missing')view.env.location.hash='';
 if(change==='origin')view.env.location.hostname='127.0.0.1';
 if(change==='query')view.env.location.search='?token=must-not-be-used';
 // Capability is captured on construction, so construct again after changing the URL.
 view.app.dispose();const app=page.createApp(view.env);await app.load();
 assert.equal(view.calls.filter(x=>x[0]==='fetch').length,0);assert.equal(view.prompts.length,0);app.dispose();
}
''')


def test_enrolment_requires_an_explicit_click_and_forwards_spki_without_a_trust_claim(tmp_path):
    run_js(tmp_path, r'''
const view=setup({enrolled:false});await view.app.load();assert.equal(view.prompts.length,0);
await view.elements.enrol.handlers.click();
assert.equal(view.prompts.length,1);assert.equal(view.prompts[0][0],'create');
assert.equal(view.prompts[0][1].publicKey.rp.id,'localhost');
const request=view.calls.find(x=>x[1]==='/consent/r_test/enrol/result');assert.ok(request);
const sent=JSON.parse(request[2].body);
assert.equal(sent.credential.response.publicKey,b64(bytes(6,7)));
assert.equal(sent.credential.response.publicKeyAlgorithm,-7);
assert.equal(sent.credential.response.attestationObject,b64(bytes(4,5)));
assert.equal(sent.credential.attested,undefined);assert.equal(sent.credential.strong,undefined);
assert.match(view.elements.status.textContent,/Hub response: enrolled/);view.app.dispose();
''')


def test_an_adapter_refusal_stays_visible_without_automatic_retries_or_prompts(tmp_path):
    run_js(tmp_path, r'''
const view=setup({overrides:{'approve/options':()=>new Response(JSON.stringify({message:'Expired or refused <script>'}),
 {status:410,headers:{'content-type':'application/json'}})}});
await view.app.load();await view.app.approve();
assert.equal(view.prompts.length,0);assert.match(view.elements.status.textContent,/Expired or refused <script>/);
assert.equal(view.calls.filter(x=>x[1]==='/consent/r_test/approve/options').length,1);
assert.equal(view.calls.filter(x=>x[1]==='/consent/r_test/approve/result').length,0);view.app.dispose();
''')


def test_cancel_during_options_prevents_even_a_late_authenticator_prompt(tmp_path):
    run_js(tmp_path, r'''
let release;const delayed=new Promise(resolve=>release=resolve);
const view=setup({overrides:{'approve/options':()=>delayed}});await view.app.load();
const pending=view.app.approve();await flush();view.app.cancel();
release(new Response(JSON.stringify({ceremony_id:'c_late',publicKey,expires:view.review.summary.expires}),{headers:{'content-type':'application/json'}}));
await pending;
assert.equal(view.prompts.length,0);
assert.equal(view.calls.filter(x=>x[0]==='fetch' && x[1].endsWith('/result')).length,0);
view.app.dispose();
''')


def test_expired_review_and_unavailable_credentials_remain_visible_without_a_fallback(tmp_path):
    run_js(tmp_path, r'''
const expired=setup();expired.review.summary.expires=1;
await expired.app.load();await expired.app.approve();
assert.equal(expired.prompts.length,0);assert.match(expired.elements.status.textContent,/current confirmed review|expired/);
assert.equal(expired.elements.approve.disabled,true);expired.app.dispose();
const unsupported=setup();unsupported.env.navigator.credentials=null;
await unsupported.app.load();await unsupported.app.approve();
assert.match(unsupported.elements.status.textContent,/no supported passkey API/);
assert.equal(unsupported.calls.filter(x=>x[1]==='/consent/r_test/approve/options').length,0);
unsupported.app.dispose();
''')


@pytest.mark.parametrize("action", ["approve", "enrol", "decline"])
def test_cancel_after_forwarding_keeps_actions_frozen_until_confirmed_status_refresh(tmp_path, action):
    run_js(tmp_path, "const action=" + repr(action) + r''';
let release;const delayed=new Promise(resolve=>release=resolve);
const suffix=action==='decline'?'decline':`${action}/result`;
const view=setup({enrolled:action!=='enrol',overrides:{[suffix]:()=>delayed}});
await view.app.load();const pending=view.app[action]();await flush();
assert.ok(view.calls.find(x=>x[1]===`/consent/r_test/${suffix}`));
view.app.cancel();
assert.match(view.elements.status.textContent,/Status unknown.*Check status/);
for(const name of ['approve','enrol','decline'])assert.equal(view.elements[name].disabled,true);
const count=view.calls.filter(x=>x[0]==='fetch').length;
await view.app.approve();await view.app.decline();await view.app.enrol();
assert.equal(view.calls.filter(x=>x[0]==='fetch').length,count);
release(new Response(JSON.stringify({status:'approved',message:'late'}),{headers:{'content-type':'application/json'}}));
await pending;
assert.match(view.elements.status.textContent,/Status unknown/);
view.review.status='unknown';await view.app.load();assert.equal(view.elements.decline.disabled,true);
view.review.status='pending';await view.app.load();assert.equal(view.elements.decline.disabled,false);
view.app.dispose();
''')


def test_timeout_after_forwarding_does_not_claim_cancelled_or_reenable_actions(tmp_path):
    run_js(tmp_path, r'''
let release;const delayed=new Promise(resolve=>release=resolve);
const view=setup({overrides:{'approve/result':()=>delayed}});
await view.app.load();const pending=view.app.approve();await flush();
const timeout=[...view.timers.values()].find(x=>x.delay===30000);assert.ok(timeout);
view.advance(30001);timeout.fn();
assert.match(view.elements.status.textContent,/Status unknown/);
assert.doesNotMatch(view.elements.status.textContent,/Cancelled|No approval/);
assert.equal(view.elements.approve.disabled,true);assert.equal(view.elements.decline.disabled,true);
release(new Response(JSON.stringify({status:'approved'}),{headers:{'content-type':'application/json'}}));
await pending;assert.match(view.elements.status.textContent,/Status unknown/);view.app.dispose();
''')


@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_pending_or_unknown_result_is_not_displayed_as_approved(tmp_path, status):
    run_js(tmp_path, "const status=" + repr(status) + r''';
const view=setup({overrides:{'approve/result':()=>new Response(JSON.stringify({status,message:'not confirmed'}),
 {headers:{'content-type':'application/json'}})}});
await view.app.load();await view.app.approve();
assert.match(view.elements.status.textContent,/Status unknown/);
assert.doesNotMatch(view.elements.status.textContent,/Hub response: approved/);
assert.equal(view.elements.approve.disabled,true);view.app.dispose();
''')


def test_challenge_review_and_secure_context_constraints_are_enforced(tmp_path):
    run_js(tmp_path, r'''
assert.throws(()=>page.requestOptions({...publicKey,challenge:b64(bytes(1,2,3,4))}));
assert.throws(()=>page.creationOptions({...publicKey,challenge:b64(new Uint8Array(33).fill(1))}));
const oversized=setup();oversized.review.canonical_review='x'.repeat(64*1024+1);
await oversized.app.load();assert.equal(oversized.elements.approve.disabled,true);
assert.match(oversized.elements.status.textContent,/oversized canonical review/);oversized.app.dispose();
const insecure=setup();insecure.env.isSecureContext=false;await insecure.app.load();
assert.equal(insecure.calls.filter(x=>x[0]==='fetch').length,0);assert.equal(insecure.prompts.length,0);
insecure.app.dispose();
''')


def test_webauthn_timeout_never_outlives_the_ceremony_or_review_expiry(tmp_path):
    run_js(tmp_path, r'''
const view=setup({overrides:{'approve/options':()=>new Response(JSON.stringify({ceremony_id:'c_short',
 publicKey:{...publicKey,timeout:120000},expires:1700000002}),{headers:{'content-type':'application/json'}})}});
await view.app.load();await view.app.approve();
assert.ok(view.prompts[0][1].publicKey.timeout<=2000);assert.ok(view.prompts[0][1].publicKey.timeout>0);
view.app.dispose();
const expired=setup({overrides:{'approve/options':()=>new Response(JSON.stringify({ceremony_id:'c_expired',
 publicKey,expires:1}),{headers:{'content-type':'application/json'}})}});
await expired.app.load();await expired.app.approve();assert.equal(expired.prompts.length,0);
assert.match(expired.elements.status.textContent,/ceremony has expired/);expired.app.dispose();
''')
