"""Private isolated tour endpoints. No operational connection or chat persistence."""
from pathlib import Path
from fastapi import APIRouter,Request,HTTPException
from fastapi.responses import HTMLResponse,FileResponse
from app.routes.wondrue_cockpit import authorize,HEADERS
from app.db import DatabaseNotConfiguredError,DatabaseUnavailableError
router=APIRouter()
ASSETS=Path(__file__).resolve().parents[1]/'wondrue_assets'


@router.get('/wondrue/demo-tour',response_class=HTMLResponse)
def page():
    return HTMLResponse((ASSETS/'demo-tour.html').read_text(encoding='utf-8'),headers=HEADERS)


@router.get('/wondrue/api/demo-tour')
def tour(request:Request):
    authorize(request)
    from app.demo_tour import service
    try:return service()
    except (DatabaseNotConfiguredError,DatabaseUnavailableError):raise HTTPException(503,'Isolated development demo storage unavailable. No operational fallback.') from None
    except ValueError as e:raise HTTPException(409,str(e)) from None


@router.get('/wondrue/api/demo-tour/reference/{id}')
def pdf(id:str,request:Request):
    authorize(request)
    names={'us':'Commercial_Office_Vacancy_2020-2026_Clickable_Bibliography.pdf','international':'International_Commercial_Office_Vacancy_2020-2026_Clickable_Bibliography.pdf'}
    if id not in names:raise HTTPException(404)
    path=ASSETS.parents[1]/'config'/'demo_story'/'references'/names[id]
    return FileResponse(path,media_type='application/pdf',filename=path.name,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
