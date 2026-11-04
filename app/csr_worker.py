"""Bounded CSR recovery worker. No discovery or unrelated agent dispatch."""
import json
from app.customer_service import drain_web
from app.csr_email import drain


if __name__=='__main__':print(json.dumps({'web':drain_web(),'email':drain()}))
