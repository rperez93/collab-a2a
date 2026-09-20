"""Installed guidance remains discoverable without loading entire runbooks."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from collab import skills
from collab.cli import build_parser


@pytest.mark.parametrize('agent,tail', [('codex','.codex/skills'),('claude-code','.claude/skills'),
                                       ('opencode','.config/opencode/skills'),('cursor','.cursor/skills')])
def test_a_real_install_copies_new_capabilities_and_their_references_into_each_host(tmp_path, agent, tail):
    """A source-only reference is useless after wheel/copy installation."""
    env={**os.environ, 'COLLAB_AGENT_HOME':str(tmp_path), 'CLAUDE_CONFIG_DIR':str(tmp_path/'.claude'),
         'COLLAB_CONFIG':str(tmp_path/'config.json'), 'COLLAB_STATE_DIR':str(tmp_path/'state'),
         'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    result=subprocess.run([sys.executable,'-m','collab.cli','skills','install','--agent',agent,'--copy'],
                          env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    installed=tmp_path/tail
    assert {'collab-worker','collab-telemetry','collab-tasks'} <= {p.name for p in installed.iterdir()}
    for path in installed.rglob('*.md'):
        for link in re.findall(r'\]\(([^)]+)\)',path.read_text()):
            if not link.startswith(('http:','https:','#')):
                assert (path.parent/link.split('#')[0]).is_file(), (path,link)
    assert (installed/'collab-telemetry/references/providers.md').is_file()


def test_the_bundled_catalog_exposes_every_entrypoint_without_private_catalog_scanning():
    catalog=skills.catalog()
    assert {entry['name'] for entry in catalog} == set(skills.SKILL_NAMES)
    assert all(entry['description'] and Path(entry['path']).is_file() for entry in catalog)
    assert len({entry['name'] for entry in catalog})==len(catalog)


def test_the_generic_instructions_point_to_every_capability_without_loading_runbooks(tmp_path):
    block=skills.instructions_block(tmp_path,'collab')
    for name in skills.SKILL_NAMES:
        assert str(tmp_path/name/'SKILL.md') in block
    assert len(block.splitlines())<60
    assert 'ON A LOOP' not in block
    assert 'worker send' in block and 'worker pending' in block


def test_the_staging_home_applies_to_claude_without_touching_the_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv('COLLAB_AGENT_HOME',str(tmp_path))
    monkeypatch.delenv('CLAUDE_CONFIG_DIR',raising=False)
    chosen=next(target for target in skills.known_targets() if target.key=='claude-code')
    assert chosen.path==tmp_path/'.claude/skills'


@pytest.mark.parametrize('args',[
    ['worker','start','--agent','claude','--scope','Coordinate accepted ownership'],
    ['worker','send','--to','bob','Verified result is ready'],
    ['worker','reply','e_example','Keep the accepted API'],
    ['worker','stats','--report','{"quotas":{"five_hour":25}}','--quota-scope','independent'],
    ['worker','stats','--source','adapter','--provider','canonical','--interval','120'],
    ['task','propose','Migration','--project','p_example','--detail','Acceptance fixture passes'],
    ['task','claim','--id','t_example','--files','src/migration.py'],
    ['task','pr','--id','t_example','--url','https://example.test/pull/123'],
    ['config','--tui'],
    ['theme','--new','my-terminal','--from','cyberpunk'],
])
def test_skill_workflow_commands_are_parsed_by_the_shipped_cli(args):
    """These documented workflows must reach real command handlers."""
    assert callable(build_parser().parse_args(args).func)
