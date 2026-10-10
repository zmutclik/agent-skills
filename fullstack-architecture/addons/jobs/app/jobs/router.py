from fastapi import APIRouter, HTTPException, Request

from app.auth.dependencies import CurrentUserDep
from app.core.database import DbSession
from app.core.templating import templates
from app.jobs.models import Job

router = APIRouter(prefix="/jobs", tags=["jobs"])

STOP_POLLING = 286   # status khusus HTMX: hentikan hx-trigger="every Ns"


@router.get("/{job_id}/status")
async def job_status(request: Request, job_id: int, user: CurrentUserDep, db: DbSession):
    job = await db.get(Job, job_id)                   # satu lookup PK
    if job is None or job.owner_id != user.id:
        raise HTTPException(status_code=404)
    finished = job.status in ("done", "failed")
    return templates.TemplateResponse(
        request=request, name="jobs/_status.html", context={"job": job, "finished": finished},
        status_code=STOP_POLLING if finished else 200,
    )
