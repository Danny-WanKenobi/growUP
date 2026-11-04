#!/usr/bin/env python3
"""Owner-run focused PostgreSQL checks + isolated demo proof; never full regression."""
from pathlib import Path
import os
import socket
import subprocess

TESTS=(
 'tests/test_dossier_coordinator.py::test_live_coordinator_interruption_and_concurrent_replay[solomon-False]',
 'tests/test_adviser_assessments.py',
 'tests/test_room_portfolio.py',
 'tests/test_engineering_completion.py::test_extreme_exponents_refuse_before_layout_arithmetic',
 'tests/test_planning_summaries.py',
 'tests/test_solomon_project.py::test_reopened_inspection_blocks_claims_and_owner_summary_stays_conditional',
 'tests/test_campaign_tracking.py::test_timeline_preserves_out_of_order_and_domain_separation',
 'tests/test_owner_work.py::test_open_project_inspection_cannot_be_filed_as_finished',
 'tests/test_elmer.py::test_different_expense_ids_with_shared_invoice_require_review_without_erasing_records',
 'tests/test_manny_review.py::test_owner_package_on_arrival_uses_latest_reports_and_preserves_unknowns',
)


def main():
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev': raise SystemExit('Run with local sudo on securecode-dev.')
    release=Path('/var/www/growup/current').resolve(strict=True)
    if release.parent!=Path('/var/www/growup/releases'): raise SystemExit('Active development release required.')
    command=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
        '-p','WorkingDirectory='+str(release),'-p','EnvironmentFile=/var/www/growup/shared/growup.env','-p','IPAddressDeny=any','-p','IPAddressAllow=localhost',
        '-p','TimeoutStartSec=180','/usr/bin/env','-u','BRAVE_API_KEY','-u','META_API_KEY','-u','BASETEN_API_KEY','-u','OPENAI_API_KEY',
        'DATABASE_NAME=growup_mvp_gate','GROWUP_PG_INTEGRATION=1','PYTHONDONTWRITEBYTECODE=1','META_SPEND_APPROVED=0','OBS_ENABLED=0',
        str(release/'.venv/bin/python'),'-B','-m','pytest',*TESTS,'-q','-p','no:cacheprovider','--tb=short','--maxfail=1']
    print('Focused tests only: dedicated growup_mvp_gate; no full suite or external provider access.',flush=True)
    subprocess.run(command,check=True)
    print('Focused PostgreSQL checks passed. Populating/proving the new isolated synthetic fixtures.',flush=True)
    subprocess.run(['/usr/bin/python3',str(release/'deployment/prove-demo-catalogue.py')],check=True)


if __name__=='__main__': main()
