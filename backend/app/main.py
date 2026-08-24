from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import call, consent, session, webhook


# app = FastAPI(
#     title='Phishig Call API',
#     description='AI 모의 보이스피싱 훈련 서비스 API',
#     version='1.0.0'
# )


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "Phishing Call Backend API"}


app.include_router(consent.router)
app.include_router(session.router)
app.include_router(call.router)
app.include_router(webhook.router)


# 이후 DB 연결 시 participant, call, report 라우터를 추가한다.
