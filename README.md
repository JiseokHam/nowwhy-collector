# nowwhy.kr collector

`nowwhy.kr`의 검색·뉴스 신호를 수집하고 검증된 결과를 사이트에 반영하는 비공개 자동화 저장소입니다.

## 실행 주기

- cron-job.org에서 30분마다 가벼운 검색 신호 확인(GPT 호출 없음)
- 새 주제가 상위 3위에 진입하거나 3시간이 지나면 전체 AI 근거 검증
- 유료 분석은 최소 1시간 간격으로 제한
- 월 예상 AI 비용이 $5.50에 가까워지면 절약 모드로 전환(기능 정지 없음)
- 절약 모드에서는 새 TOP3를 최소 4시간 간격으로 반영하고, 변화가 없어도 12시간마다 전체 검증
- GitHub 실행이 비정상적으로 멈추면 다음 30분 실행이 멈춘 작업을 교체
- 검증 결과가 부족하거나 실행이 실패하면 마지막 정상 TOP 10 보존

## GitHub Actions secrets

- `OPENAI_API_KEY`
- `NAVER_API_HUB_CLIENT_ID`
- `NAVER_API_HUB_CLIENT_SECRET`
- `SITE_UPLOAD_TOKEN`

키 값은 소스와 실행 로그에 저장하지 않습니다.

## 수동 실행

GitHub의 **Actions → Update nowwhy.kr data → Run workflow**에서 실행할 수 있습니다.

