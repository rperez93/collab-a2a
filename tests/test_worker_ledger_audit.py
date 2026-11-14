"""A terminal ledger is observed evidence, not a reconstructed complete history."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='safe ledger opens require POSIX/WSL')

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'audit-worker-ledger.py'
spec = importlib.util.spec_from_file_location('ledger_audit', SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
START = audit.timestamp('2026-11-14T04:00:00Z')
END = START+3600


def record(n=1, **extra):
    return {'ts':'2026-11-14T04:12:07Z','event':'turn','attempt':f'{n:012x}',
            'phase':'committed','outcome':'output','agent':'codex','model':'small',
            'reply_count':1,'escalation_count':0,**extra}


def write(path, rows):
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return path


def test_rotation_dedup_and_explicit_outcomes(tmp_path):
    first=record(tokens_in=10,cost_usd=.1)
    old=write(tmp_path/'worker.log.1',[first])
    current=write(tmp_path/'worker.log',[first,record(2,outcome='no-op',reply_count=0),
                   record(3,phase='reserved',outcome='skipped-local',reply_count=0)])
    result=audit.summarize([old,current],since=START,until=END)
    assert result['terminal_records_observed']==3
    assert result['provider_launch_records_observed']==2
    assert result['outcomes']['no-op']==1 and result['outcomes']['skipped-local']==1
    assert result['diagnostics']['duplicate_lines']==1
    assert result['measurements']['cost_usd']=={'known_sum':.1,'known_records':1,'unknown_records':2,'total':None}
    assert result['coverage']=='not_verified'
    assert result['recommended_cap'] is None


def test_conflicting_id_is_excluded_instead_of_counted_twice(tmp_path):
    p=write(tmp_path/'worker.log',[record(),record(outcome='failed')])
    out=audit.summarize([p],since=START,until=END)
    assert out['terminal_records_observed']==0
    assert out['diagnostics']['conflicting_attempts']==1
    assert out['coverage']=='incomplete'


def test_known_zero_differs_from_missing_and_subtotals_are_not_totals(tmp_path):
    p=write(tmp_path/'worker.log',[record(cost_usd=0,tokens_out=0)])
    out=audit.summarize([p],since=START,until=END)
    assert out['measurements']['cost_usd']['total']==0
    assert out['measurements']['tokens_in']['total'] is None
    assert out['measurements']['tokens_out']['total']==0


def test_half_open_window_and_drops_do_not_become_noops(tmp_path):
    p=write(tmp_path/'worker.log',[record(ts='2026-11-14T04:00:00Z'),record(2,ts='2026-11-14T05:00:00Z'),
                                  {'event':'dropped','count':7}])
    out=audit.summarize([p],since=START,until=END)
    assert out['terminal_records_observed']==1
    assert out['diagnostics']['reported_dropped_lines']==7
    assert out['outcomes']['no-op']==0
    assert out['coverage']=='incomplete'


def test_malformed_and_unterminated_records_mark_incomplete(tmp_path):
    p=tmp_path/'worker.log';p.write_bytes(b'not json\n'+json.dumps(record()).encode())
    out=audit.summarize([p],since=START,until=END)
    assert out['terminal_records_observed']==0
    assert out['diagnostics']['malformed_lines']==1
    assert out['diagnostics']['incomplete_lines']==1


@pytest.mark.parametrize('change',[{'attempt':None},{'attempt':'secret'}, {'phase':'unknown'},
    {'outcome':'unknown'}, {'ts':'2026-11-14T04:12:07'}, {'cost_usd':float('nan')}])
def test_invalid_records_never_invent_good_outcomes(tmp_path,change):
    p=write(tmp_path/'worker.log',[record(**change)])
    out=audit.summarize([p],since=START,until=END)
    assert out['terminal_records_observed']==0
    assert out['coverage']=='incomplete'


def test_negative_and_boolean_measurements_stay_unknown(tmp_path):
    p=write(tmp_path/'worker.log',[record(tokens_in=-1,cost_usd=True)])
    out=audit.summarize([p],since=START,until=END)
    assert out['measurements']['tokens_in']['total'] is None
    assert out['measurements']['cost_usd']['total'] is None
    assert out['diagnostics']['invalid_measurements']==2


def test_secret_fields_are_not_emitted(tmp_path):
    secret='DO_NOT_REPORT_TEXT'
    p=write(tmp_path/'worker.log',[record(prompt=secret,error=secret,summary=secret)])
    out=audit.summarize([p],since=START,until=END)
    assert secret not in json.dumps(out)
    assert 'attempt' not in out


def test_leaf_symlink_and_fifo_do_not_get_read(tmp_path):
    p=write(tmp_path/'worker.log',[record()])
    link=tmp_path/'link';link.symlink_to(p)
    with pytest.raises(OSError): audit.summarize([link],since=START,until=END)
    fifo=tmp_path/'fifo';os.mkfifo(fifo)
    with pytest.raises(ValueError,match='regular'): audit.summarize([fifo],since=START,until=END)


def test_size_line_and_record_limits_refuse_partial_success(tmp_path,monkeypatch):
    p=tmp_path/'large'
    with p.open('wb') as f: f.truncate(audit.MAX_FILE+1)
    with pytest.raises(ValueError,match='10 MiB'): audit.summarize([p],since=START,until=END)
    p.write_bytes(b'x'*(audit.MAX_LINE+1)+b'\n')
    with pytest.raises(ValueError,match='64 KiB'): audit.summarize([p],since=START,until=END)
    write(p,[record(),record(2)])
    monkeypatch.setattr(audit,'MAX_RECORDS',1)
    with pytest.raises(ValueError,match='50000'): audit.summarize([p],since=START,until=END)


def test_same_inode_and_too_many_inputs_rejected(tmp_path):
    p=write(tmp_path/'worker.log',[record()])
    with pytest.raises(ValueError,match='same file'): audit.summarize([p,p],since=START,until=END)
    with pytest.raises(ValueError,match='one or two'): audit.summarize([p,p,p],since=START,until=END)


def test_scan_budget_is_checked_between_reads(tmp_path,monkeypatch):
    p=write(tmp_path/'worker.log',[record()])
    times=iter([0,11]);monkeypatch.setattr(audit.time,'monotonic',lambda:next(times))
    with pytest.raises(ValueError,match='10 seconds'): audit.summarize([p],since=START,until=END)


def test_group_limit_is_not_swallowed_as_a_bad_record(tmp_path):
    p=write(tmp_path/'worker.log',[record(i,model=f'model-{i}') for i in range(257)])
    with pytest.raises(audit.LimitExceeded): audit.summarize([p],since=START,until=END)


def test_cli_failure_does_not_echo_log_contents_or_path(tmp_path):
    p=tmp_path/'PRIVATE-PATH';p.write_bytes(b'x'*(audit.MAX_LINE+1))
    result=subprocess.run([sys.executable,str(SCRIPT),str(p),'--since',str(START),'--until',str(END)],capture_output=True,text=True,timeout=5)
    assert result.returncode==2 and result.stdout==''
    assert 'PRIVATE-PATH' not in result.stderr


def test_empty_file_does_not_claim_known_cost_or_complete_coverage(tmp_path):
    p=write(tmp_path/'worker.log',[])
    out=audit.summarize([p],since=START,until=END)
    assert out['measurements']['cost_usd']['total'] is None
    assert out['coverage']=='not_verified'


@pytest.mark.parametrize('since,until',[(0,float('inf')),(0,0),(-1e308,1e308),(float('nan'),3)])
def test_bad_windows_are_not_rates(tmp_path,since,until):
    p=write(tmp_path/'worker.log',[])
    with pytest.raises(ValueError): audit.summarize([p],since=since,until=until)


@pytest.mark.parametrize('ts',['2026-11-14T05:00:00Z','2026-11-14T06:00:00Z','2026-11-14T03:00:00Z'])
def test_drop_marker_time_is_not_a_loss_interval(tmp_path,ts):
    p=write(tmp_path/'worker.log',[record(),{'event':'dropped','count':3,'ts':ts}])
    out=audit.summarize([p],since=START,until=END)
    assert out['coverage']=='incomplete'
    assert out['diagnostics']['reported_dropped_lines']==3


def test_large_ledger_has_bounded_cpu_and_memory_after_exec(tmp_path):
    code = '''import importlib.util,json,resource,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('ledger',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
p=Path(sys.argv[2])
with p.open('w') as f:
 for n in range(20000):
  f.write(json.dumps({'ts':'2026-11-14T04:12:07Z','event':'turn','attempt':f'{n:012x}','phase':'committed','outcome':'no-op','agent':'codex','model':'small','tokens_in':17,'tokens_out':0,'cost_usd':0,'seconds':.05,'reply_count':0,'escalation_count':0})+'\\n')
before=resource.getrusage(resource.RUSAGE_SELF);start=time.monotonic()
out=m.summarize([p],since=m.timestamp('2026-11-14T04:00:00Z'),until=m.timestamp('2026-11-14T05:00:00Z'))
after=resource.getrusage(resource.RUSAGE_SELF)
rss=(after.ru_maxrss-before.ru_maxrss)/(1024 if sys.platform=='darwin' else 1)
print(json.dumps({'records':out['terminal_records_observed'],'cpu':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,'rss_kib':rss,'wall':time.monotonic()-start}))
'''
    child=subprocess.run([sys.executable,'-c',code,str(SCRIPT),str(tmp_path/'large.log')],capture_output=True,text=True,timeout=15,check=True)
    metrics=json.loads(child.stdout)
    assert metrics['records']==20000
    assert metrics['cpu']<6 and metrics['wall']<8
    assert metrics['rss_kib']<98304
