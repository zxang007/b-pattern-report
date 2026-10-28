import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import scanner_service as scanner

class FiveCaseGateTests(unittest.TestCase):
    def test_failed_gate_stops_before_any_market_api_or_global_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'state_dir': str(root / 'state')}))
            options = argparse.Namespace(config=str(config), symbols=None, start_date=None, end_date=None)
            with patch.object(scanner, 'require_server'), patch('five_case_gate.ensure_five_cases', side_effect=RuntimeError('synthetic regression failure')) as gate, patch.object(scanner, 'PublicAPI') as api:
                with self.assertRaisesRegex(RuntimeError, 'regression failure'):
                    scanner.run_scan(options)
                gate.assert_called_once()
                api.assert_not_called()

if __name__ == '__main__':
    unittest.main()
