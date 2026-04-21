import os

import pytest
import yaml

from rttt.utils import deep_merge, load_configs


@pytest.fixture
def config_paths(tmp_path):
    home = tmp_path / 'home.yaml'
    user = tmp_path / 'user.yaml'
    local = tmp_path / 'local.yaml'
    return home, user, local


def test_deep_merge_nested_dicts():
    base = {'substitutions': {'A': 'x', 'B': 'y'}, 'mcp': True}
    override = {'substitutions': {'B': 'y2', 'C': 'z'}, 'device': 'nrf'}
    assert deep_merge(base, override) == {
        'substitutions': {'A': 'x', 'B': 'y2', 'C': 'z'},
        'mcp': True,
        'device': 'nrf',
    }


def test_deep_merge_override_wins_on_non_dict():
    assert deep_merge({'x': 1}, {'x': 2}) == {'x': 2}


def test_deep_merge_does_not_mutate_base():
    base = {'substitutions': {'A': 'x'}}
    override = {'substitutions': {'A': 'y'}}
    deep_merge(base, override)
    assert base == {'substitutions': {'A': 'x'}}


def test_load_configs_merges_all_layers(config_paths):
    home, user, local = config_paths
    home.write_text(yaml.safe_dump({
        'speed': 4000,
        'substitutions': {'GIT_SHA': {'shell': 'git rev-parse HEAD'}, 'LORA_KEY': 'home-default'},
    }))
    local.write_text(yaml.safe_dump({
        'device': 'nRF54L15_M33',
        'substitutions': {'LORA_KEY': 'project-override', 'PROJECT': 'demo'},
    }))

    merged, sources = load_configs([str(home), str(user), str(local)])

    assert merged['speed'] == 4000
    assert merged['device'] == 'nRF54L15_M33'
    assert merged['substitutions'] == {
        'GIT_SHA': {'shell': 'git rev-parse HEAD'},
        'LORA_KEY': 'project-override',
        'PROJECT': 'demo',
    }
    # Sources preserved in load order (low -> high priority).
    assert [os.path.basename(p) for p, _ in sources] == ['home.yaml', 'local.yaml']


def test_load_configs_missing_files_return_empty(config_paths):
    home, user, local = config_paths
    merged, sources = load_configs([str(home), str(user), str(local)])
    assert merged == {}
    assert sources == []


def test_load_configs_skips_non_mapping(tmp_path):
    bad = tmp_path / 'bad.yaml'
    bad.write_text('- just\n- a\n- list\n')
    good = tmp_path / 'good.yaml'
    good.write_text(yaml.safe_dump({'device': 'nrf'}))

    merged, sources = load_configs([str(bad), str(good)])

    assert merged == {'device': 'nrf'}
    assert [os.path.basename(p) for p, _ in sources] == ['good.yaml']
