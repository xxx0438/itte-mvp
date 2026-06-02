from itte.db import repository as repo

def maybe_create_approval(change_id: int, org: str, requested_by: str, decision: str):
    if decision != "review":
        return None

    return repo.create_approval(
        change_id=change_id,
        org=org,
        requested_by=requested_by,
    )
