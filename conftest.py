"""Root pytest conftest -- picked up automatically for every test file in this repo regardless
of which subdirectory pytest is pointed at. Tags every audit-log entry the test suite generates
as source="unit_test" (see audit/audit_log.py's set_source docstring) so a pytest run can never
be mistaken for real customer activity on the dashboard's live-activity stats."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from audit.audit_log import set_source

set_source("unit_test")
