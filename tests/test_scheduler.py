import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext
from io import BytesIO
import time
import urllib.error

spec = importlib.util.spec_from_file_location('runner', Path(__file__).parents[1] / 'collector/cloud_runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = patch.multiple(runner, DATA_DIR=root, FINAL_PATH=root/'final.json', STATE_PATH=root/'state.json', STATUS_PATH=root/'status.json', RAW_PATH=root/'raw.json')
        self.paths.start()
        self.addCleanup(self.paths.stop)
        runner.write_json(runner.FINAL_PATH, {'rankings': [{'rank': 1}]})
        runner.write_json(runner.STATUS_PATH, {'state': 'ok', 'lastSuccessfulAt': 'old'})
        runner.write_json(runner.RAW_PATH, {'rankings': [{'topic': 'same'}]})

    def test_hourly_and_stable(self):
        self.assertFalse(runner.needs_analysis({'lastFullEpoch': 1000, 'topics': ['same']}, ['same'], 2799)[0])
        self.assertTrue(runner.needs_analysis({'lastFullEpoch': 1000, 'topics': ['same']}, ['same'], 4600)[0])

    def test_stable_check_publishes_truthful_timestamp_without_ai(self):
        with patch.object(runner, 'needs_analysis', return_value=(False, 'stable')), patch.object(runner, 'run_collector', return_value=0) as collect, patch.object(runner, 'publish'):
            self.assertEqual(runner.main(), 0)
            collect.assert_called_once_with('--signals-only')
            status = runner.read_json(runner.STATUS_PATH, {})
            self.assertTrue(status['lastCheckedAt'].endswith('+00:00'))
            self.assertEqual(status['lastSuccessfulAt'], 'old')

    def test_failed_check_does_not_claim_freshness(self):
        with patch.object(runner, 'run_collector', return_value=1), patch.object(runner, 'publish'):
            self.assertEqual(runner.main(), 1)
            status = runner.read_json(runner.STATUS_PATH, {})
            self.assertEqual(status['state'], 'error'); self.assertNotIn('lastCheckedAt', status)

    def test_publish_failure_does_not_advance_analysis_checkpoint(self):
        with patch.object(runner, 'needs_analysis', return_value=(True, 'due')), patch.object(runner, 'run_collector', return_value=0), patch.object(runner, 'publish', side_effect=RuntimeError('upload failed')):
            with self.assertRaises(RuntimeError): runner.main()
            self.assertFalse(runner.STATE_PATH.exists())
            self.assertTrue(runner.outbox_path().exists())

    def test_duplicate_trigger_does_not_collect_or_change_timestamps(self):
        runner.write_json(runner.STATE_PATH, {'lastPublishedEpoch': int(time.time())})
        with patch.object(runner, 'run_collector') as collect:
            self.assertEqual(runner.main(), 0)
            collect.assert_not_called()
            self.assertNotIn('lastCheckedAt', runner.read_json(runner.STATUS_PATH, {}))

    def test_pending_result_retries_without_ai(self):
        runner.queue_publication({'lastFullEpoch': 123})
        with patch.object(runner, 'run_collector') as collect, patch.object(runner, 'publish') as publish:
            self.assertEqual(runner.main(), 0)
            collect.assert_not_called(); publish.assert_called_once()

    def test_upload_transient_retry_is_idempotent_and_commits_after_ack(self):
        runner.queue_publication({'lastFullEpoch': 123})
        response = BytesIO(b'{"ok":true}')
        response.status = 200
        with patch.dict(runner.os.environ, {'SITE_UPLOAD_URL':'https://example.test', 'SITE_UPLOAD_TOKEN':'test-only'}), patch.object(runner.urllib.request, 'urlopen', side_effect=[urllib.error.URLError('temporary'), nullcontext(response)]) as upload, patch.object(runner.time, 'sleep'):
            runner.publish()
            self.assertEqual(upload.call_count, 2)
            self.assertIs(upload.call_args_list[0].args[0], upload.call_args_list[1].args[0])
        self.assertFalse(runner.outbox_path().exists())
        self.assertEqual(runner.read_json(runner.STATE_PATH, {})['lastFullEpoch'], 123)

    def test_auth_failure_keeps_outbox_and_does_not_retry(self):
        runner.queue_publication({'lastFullEpoch': 123})
        with patch.dict(runner.os.environ, {'SITE_UPLOAD_URL':'https://example.test', 'SITE_UPLOAD_TOKEN':'test-only'}), patch.object(runner.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError('https://example.test',401,'unauthorized',{},None)) as upload:
            with self.assertRaises(RuntimeError): runner.publish()
            self.assertEqual(upload.call_count, 1)
        self.assertTrue(runner.outbox_path().exists())
        self.assertFalse(runner.STATE_PATH.exists())

if __name__ == '__main__':
    unittest.main()
