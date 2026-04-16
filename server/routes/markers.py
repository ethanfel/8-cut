from fastapi import APIRouter, Query

router = APIRouter()


def _db():
    from ..app import db
    return db


@router.get("/markers/{filename}")
def get_markers(filename: str, profile: str = Query("default")):
    markers = _db().get_markers(filename, profile)
    return [
        {"start_time": t, "marker_number": n, "output_path": p}
        for t, n, p in markers
    ]


@router.get("/profiles")
def get_profiles():
    return _db().get_profiles()


@router.get("/labels")
def get_labels():
    return _db().get_labels()
