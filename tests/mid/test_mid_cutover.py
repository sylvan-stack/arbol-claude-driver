"""Mid-tier selections. Write or run only in an explicitly requested testing task."""
from __future__ import annotations

import test_cutover as _support
from test_cutover import (
    Any,
    Path,
    ROOT,
    RetiredRoutes,
    json,
    os,
    subprocess,
    sys,
    tempfile,
    unittest,
)


class RetiredRoutes(_support.RetiredRoutes):
# TEST-TIER-BEGIN tests/test_cutover.py::RetiredRoutes.test_every_retired_selector_refuses_and_preserves_history
    def test_every_retired_selector_refuses_and_preserves_history(self) -> None:
        for identifier in ('zai', 'old-gateway', 'claude-zai'):
            for operation in ('models', 'usage', 'sign-in', 'sign-out', 'check', 'update', 'remove'):
                with self.subTest(identifier=identifier, operation=operation):
                    self.refuses('subscriptions', operation, identifier)
            self.refuses('run', '--subscription', identifier, 'hello')
        self.refuses('subscriptions', 'create', '--family', 'zai', '--label', 'Retired')
        self.refuses('subscriptions', 'import', 'anthropic', '--from', str(self.home / 'does-not-exist'))
        self.settings.write_text(json.dumps({**self.configuration, 'defaultSubscription': 'old-gateway'}))
        self.refuses('run', 'hello')
        self.assertEqual(json.loads((self.retired / 'record.json').read_text()), self.record)
        self.assertEqual((self.retired / 'history.json').read_text(), 'historical conversation')
        self.settings.write_text(json.dumps(self.configuration))
        listed = json.loads(self.call('subscriptions', 'list').stdout)
        self.assertEqual([item['id'] for item in listed], ['anthropic'])
        self.assertEqual(json.loads(self.call('subscriptions', 'families').stdout)[0]['id'], 'anthropic')
# TEST-TIER-END tests/test_cutover.py::RetiredRoutes.test_every_retired_selector_refuses_and_preserves_history

# TEST-TIER-BEGIN tests/test_cutover.py::RetiredRoutes.test_gateway_defaults_and_settings_fail_before_native_start
    def test_gateway_defaults_and_settings_fail_before_native_start(self) -> None:
        cases: list[dict[str, Any]] = [{'defaultSubscription': 'zai'}, {'toolPath': '/unused/claude-zai'},
                 {'subscriptions': {'zai': {}}}]
        for key in ('baseUrl', 'modelMap', 'toolEnvironment'):
            cases += [{key: 'SECRET-SENTINEL'}, {'subscriptions': {'anthropic': {'settings': {key: 'SECRET-SENTINEL'}}}}]
        for settings in cases:
            with self.subTest(settings=settings):
                self.settings.write_text(json.dumps({**self.configuration, **settings}))
                self.refuses('run', 'hello')
# TEST-TIER-END tests/test_cutover.py::RetiredRoutes.test_gateway_defaults_and_settings_fail_before_native_start

# TEST-TIER-BEGIN tests/test_cutover.py::RetiredRoutes.test_incomplete_concurrent_creation_is_not_a_sign_in_to_recover
    def test_incomplete_concurrent_creation_is_not_a_sign_in_to_recover(self) -> None:
        (self.state / 'subscriptions/anthropic-2/home').mkdir(parents=True)
        result = self.call('subscriptions', 'create', '--family', 'anthropic', '--label', 'New')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['family'], 'anthropic')
        self.assertEqual((self.retired / 'history.json').read_text(), 'historical conversation')
        self.assertFalse(self.sentinel.exists())
# TEST-TIER-END tests/test_cutover.py::RetiredRoutes.test_incomplete_concurrent_creation_is_not_a_sign_in_to_recover
