# nowwhy.kr collector

`nowwhy.kr`의 검색·뉴스 신호를 수집하고 검증된 결과를 사이트에 반영하는 비공개 자동화 저장소입니다.

## 실행 주기

- 30분마다 가벼운 검색 신호 확인 (GitHub 예약 실행은 지연될 수 있음)
- 상위 주제 변화가 크거나 1시간이 지나면 전체 AI 근거 검증
- 같은 기사 근거에 대한 기존 AI/임베딩 캐시 재사용; 비용은 신규 기사량에 따라 달라짐
- 검색 신호 확인 시각(lastCheckedAt)과 전체 분석 시각(generatedAt)을 별도 공개
- 검증 결과가 부족하거나 실행이 실패하면 마지막 정상 TOP 10 보존
- 게시 성공 후에만 분석 체크포인트 저장
- 사이트는 고유 분석 결과를 기록해 한국 시간 기준 날짜별/월별 TOP10 제공

## GitHub Actions secrets

- `OPENAI_API_KEY`
- `NAVER_API_HUB_CLIENT_ID`
- `NAVER_API_HUB_CLIENT_SECRET`
- `SITE_UPLOAD_TOKEN`

키 값은 소스와 실행 로그에 저장하지 않습니다.

## 수동 실행

GitHub의 **Actions → Update nowwhy.kr data → Run workflow**에서 실행할 수 있습니다.
