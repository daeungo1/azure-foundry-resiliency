---
title: 검증 범위와 재현 가능한 테스트
description: 로컬 contract, HTTP 및 SSE chaos matrix와 Azure 실환경 결과의 구분
ms.date: 2026-10-21
---

## 테스트 계층

| 계층 | 검증 대상 | 실제 Azure 호출 |
|------|-----------|-----------------|
| Unit/contract | config, headers, retry budget, deadline, concurrency, circuit와 복구 | 없음 |
| SDK integration | 실제 AsyncOpenAI + httpx MockTransport, deployment name, store=false, SSE | 없음 |
| Chaos matrix | 4개 모델 × same/mixed × 10조건, 요청별 실제 선택 경로 기록 | 없음 |
| Virtual subscriptions | 가상 두 구독 × N개 지역, 구독별 quota 가정과 14개 장애 조건 | 없음 |
| ARM preflight | 실제 기존 resource와 model deployment의 관리 정보 | GET만 사용 |
| Live smoke | 기존 모델 endpoint 소량 추론, upstream TTFT | 명시적 승인 후 과금 가능 |
| Global load/Agent E2E | 국가별 실제 사용자 지연과 부하, tool 및 검색 포함 | 현재 미수행 |

기본 chaos matrix는 현재 사용자 범위인 단일 구독입니다.
`simulate --subscriptions 2 --regions N`으로 가상 두 구독의 routing·quota pool 격리를 검증합니다.
실제 cross-subscription quota나 capacity isolation을 검증하지 않습니다.

## 전후 TTFT와 성공률 비교 시험

[README 전후 비교표](../README.md#적용-전후-ttft와-응답-성공률)의 원시 자료는
[ttft-comparison-sse.json](results/ttft-comparison-sse.json)입니다.
`compare`는 실제 SDK의 SSE 첫 텍스트를 고해상도 monotonic clock으로 계측하며
기존 `simulate`의 가상 정책 시간이나 Azure 실측 결과와 구분합니다.

```powershell
uv run --no-sync python -m foundry_resiliency compare --count 20 `
  --output artifacts\ttft-comparison-sse.json
```

| 비교 항목 | 적용 전 | 적용 후 |
|-----------|---------|---------|
| 후보·전환 | Subscription01/Region01 고정, 1회 시도, fallback 없음 | 두 구독·4개 지역, 사전 계측 순서, 동일/승인 모델 전환 |
| 공통 입력 | 동일한 `OK` 요청, 입력 추정 8·최대 출력 16 token | 동일 |
| 공통 시간 예산 | deadline 8초, 첫 텍스트 2초 | 동일, 총 시도 상한 20회 |
| SDK 준비·연결 | 비교 전에 초기화·warmup | 같은 client 재사용 |
| 실행 순서 | 홀수 요청은 전→후, 짝수 요청은 후→전 | 순서 효과를 줄이기 위해 교대 |

네 모델 × 정상·느린 Primary·구독 429·구독 503·요청 모델 전체 429의 다섯 조건을 비교합니다.
`--count 20`이면 전후 각각 400개, 합계 800개 비교 요청입니다.
각 모델·조건의 지역별 warmup 1회·calibration 3회는 별도 집계하여
warmup 80개·calibration 240개를 비교 성공률 분모에 섞지 않습니다.
SDK readiness warmup은 최대 120초의 별도 상한을 사용하며, 측정 요청의 2초 제한을 늘리지 않습니다.

모의 SSE는 `response.created`를 즉시 전송하고 첫 텍스트를 정상 30ms 또는 느린 Primary 250ms 뒤에 보냅니다.
실제 OS sleep과 `time.perf_counter`를 사용해 Windows의 거친 event-loop timer가 짧은 지연을 0으로 만들지 않도록 합니다.
헤더·초기 이벤트 도착을 TTFT로 세지 않는지와 지연 하한을 회귀 테스트로 확인합니다.
mock client는 환경 proxy를 사용하지 않으며 네트워크 socket이나 Azure endpoint를 호출하지 않습니다.

성공은 기대 텍스트와 SSE 완료를 모두 확인한 요청입니다.
TTFT p50/p95/최댓값은 성공 요청의 원시 표본에서 계산하고 실패율은 전체 요청으로 계산합니다.
첫 텍스트가 없는 실패의 TTFT는 `null`이며 README에는 `미수신`으로 표시합니다.
모델별 percentile을 평균하지 않고 모든 해당 요청 표본을 합쳐 nearest-rank로 집계합니다.
실패 이유·선택 모델·구독·지역·시도별 HTTP 상태와 모델 변경 여부를 보관합니다.
README의 숫자가 이 원시 결과와 일치하는지 자동 검사합니다.

사전 profile은 Subscription01의 지역별 통제된 표본으로 구성합니다.
구독별 실제 latency 동일성이나 production 자동 그룹 학습을 검증한 것이 아닙니다.
cold-start·사용자 last-mile·실제 모델 추론·동시 부하·업무 품질은 이 비교의 범위 밖입니다.
정상 행의 시간 차이와 제한된 표본을 Azure 개선율이나 SLA로 제시하지 않습니다.

## 10개 chaos 조건

| 조건 | Same 모드 기대 | Mixed 모드 기대 |
|------|----------------|-----------------|
| healthy | 동일 모델 성공 | 요청 모델 성공 |
| region_429 | 다른 지역의 동일 모델 | 동일 모델 우선 |
| same_model_429 | bounded failure | 승인 fallback 모델 성공·degraded 기록 |
| same_model_503 | bounded failure | 요청 모델 전체 장애 후 승인 모델 성공 |
| fallback_model_429 | bounded failure | 요청 모델과 첫 fallback 모두 장애이면 다음 승인 모델 |
| correlated_429 | bounded failure | 전체 모델이 막히면 bounded failure |
| primary_503 | 동일 모델 지역 전환 | 동일 모델 우선 |
| primary_timeout | 동일 모델 지역 전환 | 동일 모델 우선 |
| auth_401 | 한 번 시도 후 fail-fast | 타 모델·지역으로 권한 오류 은폐 금지 |
| headroom | 한 번 성공 후 공유 quota pool 선제 제한 | 요청 모델 pool 제한 후 승인 후보 |

HTTP와 SSE 모드를 각각 실행합니다.
`passed=true`는 이 기대 결과에 부합했다는 뜻이며 모든 요청 성공을 뜻하지 않습니다.
각 row에는 성공·실패 수, 실제 mock HTTP attempt 수, 최대 attempt, degraded 수와 model invariant가 있습니다.
`samples`에는 논리 요청마다 성공 여부, 선택 모델·지역 별칭, `output_verified`,
실패 원인과 실제 HTTP 시도 순서가 포함됩니다.
첫 텍스트 이후 stream disconnect는 replay 없이 호출자에게 전파되는지도 별도 검사합니다.

두 가상 구독에서는 `subscription_429`, `subscription_503`,
`subscription_request_headroom`, `subscription_token_headroom` 조건을 추가합니다.
한 구독의 headroom 소진이 같은 구독의 다른 지역을 차단하면서
다른 구독의 동일 모델은 차단하지 않는지 직접 예산 객체와 실제 SDK 호출을 함께 검증합니다.
결과의 `topology`에는 구독·지역 별칭, backend 수, quota pool 수와 가정을 기록합니다.
`samples`의 선택 구독과 각 HTTP attempt의 구독도 포함합니다.
현재 공유 JSON에는 단일·두 구독 모두 topology 및 시도별 구독 metadata를 포함합니다.

```powershell
uv run python -m pytest -q
uv run python -m foundry_resiliency simulate --count 10 --output artifacts\resilience-http.json
uv run python -m foundry_resiliency simulate --stream --count 10 --output artifacts\resilience-sse.json
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
az bicep build --file infra\main.bicep --outfile artifacts\main.json
```

```powershell
uv run python -m foundry_resiliency simulate --subscriptions 2 --regions 4 --count 10 `
  --output artifacts\two-subscriptions-http.json
uv run python -m foundry_resiliency simulate --subscriptions 2 --regions 4 --stream --count 10 `
  --output artifacts\two-subscriptions-sse.json
```

N은 각 구독에 배치한 지역 개수이며, 1~16 범위입니다.
N=4 테스트는 32개 backend와 subscription/model/version 기준 8개 quota pool을 사용합니다.
두 protocol 합계 2,240개 요청에서 1,680개 성공, 560개 기대된 실패를 관측했습니다.
구독 단위 429/503/요청·토큰 headroom의 네 조건에서는 640/640 성공,
모든 후보의 상관된 429 조건에서는 0/160 성공을 확인했습니다.
원시 결과는 [두 구독 HTTP](results/two-subscriptions-http.json)와
[두 구독 SSE](results/two-subscriptions-sse.json)에 있습니다.
모든 성공은 텍스트·stream 완료까지 검증하며 실제 Azure 호출은 없습니다.

시도 상한은 최대 20회를 유지합니다.
정상 후보까지 필요한 시도가 상한을 넘는 큰 N에서는 기대 결과에 `attempt_budget` 실패가 포함됩니다.
각 요청은 독립적인 시도 예산을 가지며 후속 요청은 같은 router의 cooldown 상태를 재사용합니다.
이 연속 요청 테스트는 처리량 또는 동시 부하 성능 시험이 아닙니다.

## 실제 검증과 결과 저장

2026-10-20(KST)에 단일/두 구독의 HTTP·SSE 네 결과를 모두 다시 생성했습니다.
기본 집계는 384개 조건·3,840개 요청, 2,648개 성공·1,192개 기대된 실패로 유지됐습니다.
N=8·16 보조 검증에서도 20회 시도 상한과 기록된 연속 요청 동작이 재현됐습니다.
전체 회귀 테스트 92개와 문서의 비교 지표·원시 결과 정합성도 재확인합니다.

로컬 실측 결과는 [verification.json](verification.json)에 기록합니다.
모의 TTFT는 SDK·로컬 코드의 시간이며 모델·네트워크 성능 증거가 아닙니다.
공유 가능한 모의 장애 결과는 [HTTP JSON](results/resilience-http.json)과
[SSE JSON](results/resilience-sse.json)에 저장했습니다.
각각 80개 조건/800개 요청에서 484개 성공, 316개 기대된 실패를 관측했습니다.
README의 집계 표와 저장된 JSON이 일치하는지 회귀 테스트로 확인합니다.
다시 실행한 결과나 실환경 상세 JSON은 `artifacts`에 저장하고 고객 데이터나 키를 넣지 않습니다.
공유 결과는 실제 endpoint·국가·Azure location 없이 Region01~Region04만 포함합니다.
실험 정책은 결과의 `policy`에 포함되며, 60초 backoff와 고정 jitter 샘플로 반복 호출 수를 일정하게 합니다.
실험은 15초 deadline·10초 attempt/첫 텍스트 제한을 사용합니다.
`timing_mode=virtual_policy_clock`으로 정책 시계를 분리하여 SDK 초기화·호스트 스케줄링 지연이
모의 remaining/reset·cooldown·deadline 판정을 바꾸지 않도록 합니다.
가상 시계는 모의 대기에서만 진행하며, 즉시 응답하는 모의 요청의 `first_token_ms=0`은
실제 Azure TTFT가 0이라는 의미가 아닙니다. `elapsed_ms`는 호스트의 실제 실험 경과 시간입니다.
실제 라우터는 그대로 monotonic clock과 asyncio timeout을 사용하며,
실제 timeout·deadline과 동시성·취소 동작은 별도 회귀 테스트로 검증합니다.
모의 결과로 latency 목표 합격이나 부하 시험을 주장하지 않습니다.
pytest의 fake clock 테스트는 긴 Retry-After를 실제로 기다리지 않고 규칙을 검증합니다.
SSE 테스트는 HTTP chunk를 텍스트 토큰으로 잘못 계산하지 않습니다.

## Streaming TTFT 그룹의 수용 기준

현재 TTFT 호출 경로는 실제 OpenAI SDK의 `stream=True`와 모의 SSE를 사용합니다.
첫 non-empty `response.output_text.delta`를 기다리며 `response.created`를 TTFT로 계산하지 않습니다.
기존 SSE 원시 결과는 단일 구독 800개와 두 가상 구독 1,120개, 합계 1,920개 논리 요청을 포함합니다.
이는 장애 전환·stream 완료 검증이며 실제 Azure 리전 성능이나 TTFT 그룹 효과를 측정한 결과는 아닙니다.

TTFT 통계 기반 자동 그룹은 현재 미구현입니다.
운영 플랫폼에 적용하기 전 다음 조건을 추가 검증하며, 이 표를 신규 시험의 통과 결과로 해석하지 않습니다.

| 수용 조건 | 검증 방법 | 현재 근거 또는 추가 작업 |
|-----------|-----------|--------------------------|
| 헤더·빈 이벤트가 아닌 첫 텍스트 계측 | 초기 이벤트 이후 텍스트를 지연시키는 SSE와 실제 사용자 trace 비교 | 기존 SDK 첫 텍스트 판정 검사, 실제 지연·사용자 계측 추가 |
| 첫 텍스트 전 장애 전환·이후 replay 금지 | 429·timeout·stream disconnect 주입, 실제 wire 시도 수 확인 | 기존 SSE·회귀 테스트 |
| 빠른 후보도 예산 소진 시 제외 | TTFT profile과 remaining requests/tokens를 함께 주입 | 기존 admission 근거에 그룹 선택 결합 시험 추가 |
| 가까운 리전보다 실측 우수 후보 선택 | 같은 업무·모델에서 endpoint별 지연을 통제하고 선택 확인 | profile 기반 선택 기능·시험 추가 |
| 통계 부족·만료·관측 장애 시 안전한 복귀 | 표본·측정 시각·관측 장애를 주입하고 승인된 정적 순서 확인 | 관측 상태·알림·복귀 시험 추가 |
| 지연 순위 진동·최저 후보 집중 방지 | 부하·지연을 교대로 바꾸고 분산·승격 유예·동시성 상한 확인 | 동적 제어·동시 부하 시험 추가 |
| 보안·안전성 정책 유지 | 빠르지만 허용 범위 밖인 후보와 safety refusal 입력 | 정책 도메인 필터 추가, terminal 오류 정책 유지 |
| 전환을 포함한 사용자 SLO 충족 | 실제 접점에서 정상·장애·복구와 warm/cold·부하별 비교 | 승인된 실환경 측정 필요 |

실제 endpoint별 순위는 동일 조건의 Streaming 요청을 endpoint마다 고정해 수집합니다.
그 후 자동 전환을 켠 전체 Agent 경로에서 TTFT 목표 내 유효 응답률, 실패율, 품질과 비용을 함께 비교합니다.
CLI의 소량 `smoke --stream`은 경로 점검이며 글로벌 TTFT matrix·동시 부하·그룹 자동 생성 검증을 대신하지 않습니다.
자세한 계측 단위·표본 관리·호출 시간 예산은 [TTFT BP](latency.md)에 있습니다.

## SVG 검증

SVG는 XML·접근성 검사와 별도로 브라우저에서 실제 글자 경계를 측정합니다.
전체 viewBox뿐 아니라 각 라벨이 속한 가장 작은 박스에 대해 SVG 좌표 기준 좌우 12, 상하 8의 여백을 확인합니다.
Segoe UI, Arial, sans-serif 각각에서 `document.fonts.ready`를 기다린 후
박스 밖 글자와 같은 박스 내 텍스트 겹침도 검사합니다.
이 브라우저 치수 검증은 pytest의 정적 SVG 검사와 구분하며, 결과는 검증 기록의 `svg_render`에 남깁니다.
연결선도 실제 경로를 따라 측정하여 라벨과의 교차 및 3단위 여백을 확인합니다.
선의 굵기와 화살촉 크기를 포함하여 직교 선분과 글자 경계의 교차를 검사합니다.
분기·합류용 선과 화살표를 분리하고, 화살표는 하나의 연속 경로에서 명시한 target 박스의 포트로 향하게 합니다.
아홉 SVG의 47개 화살표 target 간격은 SVG 좌표 기준 6으로 통일했으며,
이 경로·target·간격은 pytest 회귀 테스트로도 확인합니다.
README의 TL;DR 6개 링크가 같은 번호의 상세 소제목으로 이어지는지,
호출 전략 SVG에도 1~6의 방안이 모두 포함되는지 검사합니다.

`smoke`는 `--confirm-paid` 없이는 호출하지 않습니다.
실제 endpoint가 없다면 live test는 `not_run`으로 남깁니다.
preflight는 quota Scope·사용 가능 capacity·전체 Agent 기능을 검증하지 않습니다.

## 실환경 시험의 안전 조건

실제 SLO는 업무별로 합의합니다. 예를 들어 국가별 p95 TTFT 상한, deadline 내 성공률,
429 비율, failover 추가 지연, degraded 응답 품질·비용, 복구 시 재진입 비율을 따로 정의합니다.
성공 요청만 모아 latency를 계산하지 말고 timeout과 실패율을 함께 보고합니다.

실제 장애 테스트는 전용 test deployment, 비용 상한과 중단 조건 아래 수행합니다.
HTTP 429는 로컬 fault injection, 제한된 test proxy, 실제 service 관측 중 무엇인지 구분합니다.
실제 shared resource pool을 고갈시키는 시험은 수행하지 않습니다.
모든 모델의 실제 context·tools·안전성·언어 품질 평가는 별도 검증 대상입니다.

## 개발 환경 제한 처리

초기 dependency restore에서 `files.pythonhosted.org` TLS handshake가 실패할 수 있습니다.
인증서 검증을 끄거나 `trusted-host`로 우회하지 마세요.
이 경우 네트워크를 수정한 뒤 `uv sync`를 재실행합니다.
이미 승인된 Python 환경을 사용할 때는 버전과 validation 방법을 결과에 명시합니다.
의존성 설치 실패를 테스트 성공으로 기록하지 않습니다.

이 작업에서는 시스템의 기존 승인 package feed를 사용해 프로젝트 `.venv`에 의존성을 복원했습니다.
공유 Python 설치나 인증서 검증 설정은 변경하지 않았습니다.
현재 workspace의 검증 환경을 그대로 재사용하려면 자동 dependency sync 없이 실행할 수 있습니다.

```powershell
uv run --no-sync python -m pytest -q
uv run --no-sync python -m foundry_resiliency simulate --stream --output artifacts\streaming.json
```

기본 public index의 `uv sync` 성공과 위 복원 경로의 성공은 구분합니다.
외부 배포 가능한 dependency lock은 생성하지 않았으며, 실제 검증 버전은 결과 JSON에 기록합니다.

### SDK 초기화와 cold-start 판정

2026-10-21 문서 검증 중 asset·SDK 테스트만 새 프로세스에서 실행하면 첫 SDK resource 초기화가
해당 테스트의 짧은 attempt/첫 텍스트 예산을 소진하여 모의 HTTP 전송 전에 timeout되는 현상을 재현했습니다.
같은 테스트를 Responses SDK resource 선로딩 후 실행한 진단에서는 17개가 통과했습니다.
코드·설정·timeout은 변경하지 않았으며, 이를 cold-start 실패 수정이나 실제 Azure TTFT 합격으로 기록하지 않습니다.
단독 실행 순서에 따른 SDK 초기화 영향은 별도 테스트 안정화 대상입니다.

운영에서는 SDK 초기화를 readiness 이전에 수행하고 client·연결을 재사용하되,
첫 사용자 요청의 cold-start도 독립적인 시험 조건으로 유지합니다.
warm 테스트 결과만으로 cold TTFT를 설명하거나 production deadline을 임의로 늘리지 않습니다.

## TTFT BP 문서 반영 후 확인

2026-10-21에 운영 코드·설정·기존 테스트를 변경하지 않은 상태에서 전체 pytest 92개가 통과했습니다.
추가로 두 가상 구독·4개 지역·Streaming·조건별 3회 요청을 실행하여
112개 조건의 336개 요청에서 252개 성공과 84개 기대된 실패를 확인했습니다.
모든 조건이 기대 결과를 만족했고, `timing_mode=virtual_policy_clock`, 실제 Azure 호출은 0회였습니다.
이 재확인은 SSE 장애 전환 검증이며 실측 TTFT 순위·새 그룹 선택 기능·운영 SLO 합격을 뜻하지 않습니다.
2026-10-20의 기본 네 결과와 README 비교 집계는 변경하지 않았습니다.

## README 요약 및 전후 비교 추가 검증

2026-10-21에 TL;DR 6개 링크·번호별 소제목·추가 SVG·전후 비교표를 검증하는 asset 테스트 14개와
전체 회귀 테스트 101개가 통과했습니다. Ruff·format·mypy 및 wheel build도 통과했습니다.
전체 pytest는 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`과 `-p anyio.pytest_plugin`을 사용해
프로젝트의 asyncio 테스트에 필요한 plugin만 활성화했습니다.
고정 endpoint 대비 BP 비교 800개 요청은 기존 3,840개 장애 행렬과 별도 결과입니다.
새 SVG는 세 글꼴에서 102개 박스 라벨·135개 canvas 라벨·21개 target port·42개 선분·21개 화살촉을 검사했고,
박스 넘침·글자 겹침·글자와 선의 교차 없이 통과했습니다.
기존 네 SVG는 변경하지 않았으며, 다섯 SVG 전체의 23개 화살표는 정적 회귀 검사를 통과했습니다.

## 번호별 workflow 배치와 게시 검증

README 1~6번에 각 방안의 workflow를 inline 이미지로 배치했습니다.
4·5번은 기존 동일 모델·혼합 모델 SVG를 재사용하고,
1·2·3·6번은 정책 필터·TTFT profile·quota admission·재시도/복구 SVG를 추가했습니다.
전체 SVG는 9개이며 47개 화살표의 연결 대상과 6단위 포트 간격을 검사합니다.
각 번호 섹션에서 해당 SVG가 링크가 아닌 이미지로 표시되는지 별도 회귀 테스트로 확인합니다.
전체 테스트 102개, asset 테스트 15개, Ruff·format·mypy와 wheel build가 통과했습니다.
아홉 SVG 전체를 별도 Chrome profile의 실제 렌더링으로 점검했습니다.
Segoe UI·Arial·sans-serif 27개 조합에서 박스 라벨 537개, canvas 라벨 687개,
target port 141개, 선분 237개, 화살촉 141개를 검사했고
박스 넘침·글자 겹침·글자와 선의 교차는 없었습니다.
CI의 evidence 단계에서도 `compare --count 3`을 실행하여 `resilience-evidence` artifact에
전후 Streaming 비교 JSON을 포함합니다. 이 축약 실행은 README의 조건별 20회 원시 결과와 별도입니다.
