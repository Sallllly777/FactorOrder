"""Portable regression checks for the published evidence and honest scope labels."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/'research/evolution_v3'
spec=importlib.util.spec_from_file_location('verify_evolution_submission',PACKAGE/'verify_submission.py')
verifier=importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class PublishedEvolutionEvidenceTests(unittest.TestCase):
    def test_published_decisions_and_counts(self):
        result=verifier.verify()
        self.assertEqual(result['replayed_full_decisions'],70)
        self.assertEqual(result['independent_validation'],'not_completed')

    def tamper(self,mutate,expected):
        temp_root=ROOT/'tests/.tmp';temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            package=Path(tmp)/'evidence'
            shutil.copytree(PACKAGE,package,ignore=shutil.ignore_patterns('__pycache__'))
            path=package/'data/evidence.json';data=json.loads(path.read_text(encoding='utf-8'))
            mutate(data);path.write_text(json.dumps(data),encoding='utf-8')
            # Updating the hash alone must not bypass semantic checks.
            manifest_path=package/'manifest.json';manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['files']['data/evidence.json']=hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,expected):verifier.verify(package)

    def test_partial_run_cannot_be_relabelled_completed(self):
        self.tamper(lambda d:next(r for r in d['runs'] if r['run']=='C29').update(status='completed'),'scope differ')

    def test_original_program_promotion_cannot_be_rewritten(self):
        self.tamper(lambda d:next(r for r in d['candidates'] if r['id']=='A29_g2_s010')['decision'].update(promoted=False),'mismatch')

    def test_program_promotion_is_not_independent_validation(self):
        self.tamper(lambda d:d['audit'].update(independently_validated_promotions=['A29_g2_s010']),'provisional')


if __name__=='__main__':unittest.main()
