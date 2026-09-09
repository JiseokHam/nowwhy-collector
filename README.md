# nowwhy.kr collector

`nowwhy.kr`의 검색·뉴스 신호를 수집하고 검증된 결과를 사이트에 반영하는 비공개 자동화 저장소입니다.

## 실행 주기

- 30분마다 가벼운 검색 신호 확인
- 상위 주제 변화가 크거나 2시간이 지나면 전체 AI 근거 검증
- 검증 결과가 부족하거나 실행이 실패하면 마지막 정상 TOP 10 보존

## GitHub Actions secrets

- `OPENAI_API_KEY`
- `NAVER_API_HUB_CLIENT_ID`
- `NAVER_API_HUB_CLIENT_SECRET`
- `SITE_UPLOAD_TOKEN`

키 값은 소스와 실행 로그에 저장하지 않습니다.

## 수동 실행

GitHub의 **Actions → Update nowwhy.kr data → Run workflow**에서 실행할 수 있습니다.


