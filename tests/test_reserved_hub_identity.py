"""Only the server's event kind conveys authority; a label never does."""
import pytest

from collab.names import is_reserved_participant_name


RESERVED = [
    'collab', 'COLLAB', ' CoLlAb ', '[collab]', 'c.o-l_l a b', 'coIIab', 'co11ab',
    'c0llab', 'co\u200bllab', 'co\u2060llab', 'c\u034follab', 'cólláb',
    'ｃｏｌｌａｂ', '𝐜𝐨𝐥𝐥𝐚𝐛', '𝕔𝕠𝕝𝕝𝕒𝕓', 'ϲollab', 'сollab', 'cоllab',
    'coӏӏab', 'coІІab', 'collаb', 'collaЬ', 'collaь', 'collaВ', 'collaв',
    'соӏӏаЬ', 'cοllαb', 'collaΒ', 'coǁab', 'ꭃllab', '\U0001043dollab',
]


@pytest.mark.parametrize('name', RESERVED)
def test_a_reserved_lookalike_is_refused_before_a_join_spends_its_invite(client, session, name):
    """A one-use invite remains usable after refusing a hub-label impersonator."""
    assert is_reserved_participant_name(name)
    before = len(session['store'].participants())
    session['store'].add_invite('single-use', max_uses=1)
    body = {'protocol_major': 2, 'version': '2.0.0', 'invite': 'single-use', 'name': name}
    response = client.post('/ext/collab/v1/join', json=body)
    assert response.status_code == 400, response.text
    assert len(session['store'].participants()) == before
    body['name'] = 'permitted guest'
    assert client.post('/ext/collab/v1/join', json=body).status_code == 200


@pytest.mark.parametrize('name', RESERVED)
def test_a_reserved_rename_leaves_the_original_identity_untouched(client, session, host_headers, name):
    """Host privileges do not include impersonating the hub's reserved label."""
    host = session['store'].participant_for_token(session['host_token'])
    response = client.post('/ext/collab/v1/rename', headers=host_headers, json={'name': name})
    assert response.status_code == 400, response.text
    assert session['store'].participant_by_id(host.id).name == 'alice'


@pytest.mark.parametrize('name', ['Ángela', '你好', 'София', 'علي', 'co-worker', 'collaboration', 'collab-helper', 'コラボ', '🙂'])
def test_legitimate_unicode_names_remain_labels_without_being_rewritten(client, session, name):
    """Reserving one visual label is not a script or Unicode naming ban."""
    assert not is_reserved_participant_name(name)
    response = client.post('/ext/collab/v1/join', json={'protocol_major': 2, 'version': '2.0.0',
        'invite': session['invite'], 'name': name})
    assert response.status_code == 200, response.text
    assert any(person.name == name for person in session['store'].participants())


def test_host_creation_and_internal_store_rename_cannot_bypass_the_reserved_label(session):
    """Host/bootstrap callers share the same insertion and rename invariant."""
    store = session['store']
    host = store.participant_for_token(session['host_token'])
    with pytest.raises(ValueError, match='reserved'):
        store.add_participant('collab', 'synthetic', is_host=True)
    with pytest.raises(ValueError, match='reserved'):
        store.rename(host.id, 'соӏӏаЬ')
    assert store.participant_by_id(host.id).name == 'alice'


def test_supplementary_confusables_are_codepoints_not_surrogate_fragments():
    """JSON surrogate literals must not silently break the generated Python map."""
    from collab.names import _LOOKALIKE
    assert all(not 0xD800 <= ord(char) <= 0xDFFF for char in _LOOKALIKE)
    assert _LOOKALIKE['\U0001043d'] == 'c'


def test_every_extracted_official_target_is_reserved_in_the_corresponding_label_position():
    """Multi-letter skeletons such as co and ll are not only single-letter substitutions."""
    from collab.names import _CONFUSABLES
    for target, characters in _CONFUSABLES.items():
        offset = 'collab'.index(target)
        for char in characters:
            value = 'collab'[:offset] + char + 'collab'[offset + len(target):]
            assert is_reserved_participant_name(value), (char, target, value)
