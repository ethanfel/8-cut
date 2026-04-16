from fastapi import APIRouter, Query

router = APIRouter()


def _db():
    from ..app import db
    return db


@router.post("/hidden/{filename}")
def hide_file(filename: str, profile: str = Query("default")):
    _db().hide_file(filename, profile)
    return {"hidden": filename}


@router.delete("/hidden/{filename}")
def unhide_file(filename: str, profile: str = Query("default")):
    _db().unhide_file(filename, profile)
    return {"unhidden": filename}


@router.get("/hidden")
def get_hidden(profile: str = Query("default")):
    return sorted(_db().get_hidden_files(profile))
