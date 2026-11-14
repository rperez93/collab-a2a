"""The actual enrol module under synthetic browser APIs, never a real credential."""
from pathlib import Path
import shutil
import subprocess

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "src/collab/client/passkey_assets"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node is absent")

HARNESS = r'''
import assert from 'node:assert/strict';
import * as enrol from './enrol.mjs';
import {encodeBinary} from './passkey.mjs';
const bytes=(...values)=>Uint8Array.from(values).buffer;
const b64=value=>encodeBinary(value,8192);
const makeKey=()=>({id:b64(bytes(1,2,3)),rawId:bytes(1,2,3),type:'public-key',
 response:{getPublicKey:()=>bytes(5,6,7),getPublicKeyAlgorithm:()=>-7,
  get attestationObject(){assert.fail('attestation must not be read or forwarded');}}});
const makeAssertion=()=>({id:b64(bytes(1,2,3)),rawId:bytes(1,2,3),type:'public-key',
 response:{clientDataJSON:bytes(8,9,10),authenticatorData:bytes(11,12),signature:bytes(13,14)}});
function setup({credentials={},overrides={},participant='p_original'}={}) {
 let wall=1700000000000,mono=0,timer=0,phase='ready';
 const elements={},calls=[],prompts=[],timers=new Map();
 for(const id of ['status','session','participant-id','participant-name','trust','credential','usable-at','create','prove','refresh','cancel']) {
  elements[id]={textContent:'',dataset:{},disabled:false,addEventListener(){}};
  Object.defineProperty(elements[id],'innerHTML',{set(){assert.fail('untrusted HTML sink');}});
 }
 const metadata={credential_id:b64(bytes(1,2,3)),algorithm:-7,fingerprint:'a'.repeat(64),
  trust:'tofu-unverified',state:'active',created_at:wall/1000,usable_at:wall/1000+600};
 const options={challenge_id:'enrol_c',expires_at:wall/1000+30,publicKey:{
  challenge:b64(new Uint8Array(32).fill(4)),rp:{id:'localhost',name:'Collab'},
  user:{id:b64(new TextEncoder().encode(participant)),name:participant,displayName:'<img src=x onerror=bad>'},
  pubKeyCredParams:[{type:'public-key',alg:-7},{type:'public-key',alg:-257}],timeout:30000}};
 const env={document:{getElementById:id=>elements[id]},isSecureContext:true,
  location:{pathname:'/enrol/flow_test',hostname:'localhost',hash:'#'+'A'.repeat(43),search:''},
  history:{replaceState(...args){calls.push(['strip',args]);}},
  navigator:{credentials:{create:async value=>{prompts.push(['create',value]);return makeKey();},
   get:async value=>{prompts.push(['get',value]);return makeAssertion();},...credentials}},
  AbortController,Date:class extends Date{static now(){return wall;}},performance:{now:()=>mono},
  setTimeout(fn,delay){timers.set(++timer,{fn,delay});return timer;},clearTimeout(id){timers.delete(id);},
  fetch:async(url,request)=>{
   calls.push(['fetch',url,request]);
   const action=url.split('/').at(-1);
   if(overrides[action])return overrides[action](request);
   let result;
   if(action==='status')result={status:phase,session_id:'s_test',participant_id:participant,
    display_name:'<img src=x onerror=bad>',trust:'tofu-unverified',credential:phase==='enrolled'?metadata:null};
   else if(action==='options'){phase='ceremony';result=options;}
   else {phase='enrolled';result={status:'enrolled',credential:metadata};}
   return new Response(JSON.stringify(result),{headers:{'content-type':'application/json'}});
  }};
 const app=enrol.createEnrolApp(env);
 return {app,env,elements,calls,prompts,options,metadata,timers,
  setPhase(value){phase=value;},advance(ms){wall+=ms;mono+=ms;}};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const finishCalls=s=>s.calls.filter(row=>row[0]==='fetch'&&row[1].endsWith('/finish'));
'''


def run_js(tmp_path, body):
    shutil.copyfile(ASSETS / "passkey.js", tmp_path / "passkey.mjs")
    (tmp_path / "enrol.mjs").write_text((ASSETS / "enrol.js").read_text().replace('"./passkey.js"', '"./passkey.mjs"'))
    script = tmp_path / "enrol-contract.mjs"
    script.write_text(HARNESS + "\n" + body)
    done = subprocess.run([NODE, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=8)
    assert done.returncode == 0, done.stdout + done.stderr


def test_creation_and_possession_are_separate_explicit_steps_and_attestation_is_not_forwarded(tmp_path):
    run_js(tmp_path, r'''
const s=setup();await s.app.load();
assert.equal(s.prompts.length,0);assert.equal(s.calls[0][0],'strip');
assert.equal(s.elements['participant-name'].textContent,'<img src=x onerror=bad>');
await s.app.create();
assert.deepEqual(s.prompts.map(row=>row[0]),['create']);assert.equal(finishCalls(s).length,0);
const creation=s.prompts[0][1].publicKey;
assert.deepEqual(new Uint8Array(creation.user.id),new TextEncoder().encode('p_original'));
assert.equal(creation.attestation,'none');assert.equal(creation.authenticatorSelection.userVerification,'required');
await s.app.prove();
assert.deepEqual(s.prompts.map(row=>row[0]),['create','get']);
const get=s.prompts[1][1].publicKey;
assert.equal(get.rpId,'localhost');assert.equal(get.userVerification,'required');
assert.equal(get.allowCredentials.length,1);assert.equal(b64(get.allowCredentials[0].id),b64(bytes(1,2,3)));
assert.equal(b64(get.challenge),s.options.publicKey.challenge);
assert.equal(finishCalls(s).length,1);
const sent=JSON.parse(finishCalls(s)[0][2].body);
assert.deepEqual(Object.keys(sent).sort(),['challenge_id','created_key','credential']);
assert.equal(sent.created_key.public_key_spki,b64(bytes(5,6,7)));
assert.deepEqual(sent.credential.response,{clientDataJSON:b64(bytes(8,9,10)),authenticatorData:b64(bytes(11,12)),signature:b64(bytes(13,14))});
assert.equal(s.elements.create.disabled,true);assert.equal(s.elements.prove.disabled,true);
assert.match(s.elements.status.textContent,/cooldown/);assert.match(s.elements['usable-at'].textContent,/T/);
''')


@pytest.mark.parametrize("fault", ["missing-export", "null-spki", "algorithm", "id-mismatch", "oversized-spki"])
def test_an_unusable_created_key_never_reaches_the_possession_or_finish_step(tmp_path, fault):
    run_js(tmp_path, r'''
const key=makeKey();
const fault=FAULT;
if(fault==='missing-export')delete key.response.getPublicKey;
if(fault==='null-spki')key.response.getPublicKey=()=>null;
if(fault==='algorithm')key.response.getPublicKeyAlgorithm=()=>-8;
if(fault==='id-mismatch')key.id='different';
if(fault==='oversized-spki')key.response.getPublicKey=()=>new Uint8Array(4097).buffer;
const s=setup({credentials:{create:async()=>key}});await s.app.load();await s.app.create();await s.app.prove();
assert.equal(finishCalls(s).length,0);assert.equal(s.prompts.length,0);assert.equal(s.elements.prove.disabled,true);
'''.replace("FAULT", repr(fault)))


def test_a_different_possession_credential_does_not_get_submitted(tmp_path):
    run_js(tmp_path, r'''
const other=makeAssertion();other.id=b64(bytes(99));other.rawId=bytes(99);
const s=setup({credentials:{get:async()=>other}});await s.app.load();await s.app.create();await s.app.prove();
assert.equal(finishCalls(s).length,0);assert.match(s.elements.status.textContent,/differs/);
''')


def test_a_lost_finish_requires_positive_status_before_any_other_ceremony(tmp_path):
    run_js(tmp_path, r'''
const s=setup({overrides:{finish:async()=>{throw new Error('synthetic lost response');}}});
await s.app.load();await s.app.create();await s.app.prove();
assert.equal(finishCalls(s).length,1);assert.equal(s.elements.create.disabled,true);assert.equal(s.elements.prove.disabled,true);
s.setPhase('uncertain');await s.app.load();await s.app.create();await s.app.prove();
assert.equal(finishCalls(s).length,1);
s.setPhase('enrolled');await s.app.load();assert.match(s.elements.status.textContent,/registered/);
assert.equal(s.elements.create.disabled,true);assert.equal(s.elements.prove.disabled,true);
''')


def test_expiry_after_an_authenticator_await_prevents_forwarding(tmp_path):
    run_js(tmp_path, r'''
let s; s=setup({credentials:{get:async()=>{s.advance(31000);return makeAssertion();}}});
await s.app.load();await s.app.create();await s.app.prove();
assert.equal(finishCalls(s).length,0);assert.match(s.elements.status.textContent,/expired/);
''')


def test_cancelled_or_superseded_authenticator_callbacks_cannot_finish(tmp_path):
    run_js(tmp_path, r'''
let release;const pending=new Promise(resolve=>{release=resolve;});
const s=setup({credentials:{get:()=>pending}});await s.app.load();await s.app.create();
const running=s.app.prove();await flush();s.app.cancel();release(makeAssertion());await running;
assert.equal(finishCalls(s).length,0);assert.match(s.elements.status.textContent,/Cancelled locally/);
''')


def test_a_participant_id_that_cannot_fit_a_webauthn_handle_never_prompts(tmp_path):
    run_js(tmp_path, r'''
const s=setup({participant:'p_'+'x'.repeat(63)});await s.app.load();await s.app.create();
assert.equal(s.prompts.length,0);assert.equal(finishCalls(s).length,0);
assert.equal(s.elements.create.disabled,true);
''')


def test_hub_expiry_aborts_a_stuck_creation_prompt_and_ignores_its_late_result(tmp_path):
    run_js(tmp_path, r'''
let release;const pending=new Promise(resolve=>{release=resolve;});
const s=setup({credentials:{create:()=>pending}});await s.app.load();
const running=s.app.create();await flush();
assert.equal(s.timers.size,1);const timer=[...s.timers.values()][0];assert.equal(timer.delay,30000);
timer.fn();release(makeKey());await running;
assert.equal(finishCalls(s).length,0);assert.equal(s.elements.prove.disabled,true);
''')


@pytest.mark.parametrize("state", ["frozen", "retired"])
def test_a_registered_but_inactive_key_is_not_presented_as_merely_waiting_for_cooldown(tmp_path, state):
    run_js(tmp_path, r'''
const s=setup();s.metadata.state=STATE;s.setPhase('enrolled');await s.app.load();
assert.match(s.elements.status.textContent,/cannot approve operations/);
assert.match(s.elements.credential.textContent,new RegExp('^'+STATE+':'));
assert.equal(s.elements.create.disabled,true);assert.equal(s.elements.prove.disabled,true);
'''.replace("STATE", repr(state)))
