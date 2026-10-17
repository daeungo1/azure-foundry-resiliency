---
title: 검증 범위와 재현 가능한 테스트
description: 로컬 contract, HTTP 및 SSE chaos matrix와 Azure 실환경 결과의 구분
ms.date: 2026-10-16
---

## 테스트 계층

| 계층 | 검증 대상 | 실제 Azure 호출 |
|------|-----------|-----------------|
| Unit/contract | config, headers, retry budget, deadline, concurrency, circuit와 복구 | 없음 |
| SDK integration | 실제 AsyncOpenAI + httpx MockTransport, deployment name, store=false, SSE | 없음 |
| Chaos matrix | 4개 모델 × same/mixed × 8조건, 여러 논리 요청 | 없음 |
| ARM preflight | 실제 기존 resource와 model deployment의 관리 정보 | GET만 사용 |
| Live smoke | 기존 모델 endpoint 소량 추론, upstream TTFT | 명시적 승인 후 과금 가능 |
| Global load/Agent E2E | 국가별 실제 사용자 지연과 부하, tool 및 검색 포함 | 현재 미수행 |

기본 chaos matrix는 현재 사용자 범위인 단일 구독입니다.
다중 구독은 configuration generation contract와 SVG 참고안만 제공합니다.
실제 cross-subscription quota나 capacity isolation을 검증하지 않습니다.

## 8개 chaos 조건

| 조건 | Same 모드 기대 | Mixed 모드 기대 |
|------|----------------|-----------------|
| healthy | 동일 모델 성공 | 요청 모델 성공 |
| region_429 | 다른 지역의 동일 모델 | 동일 모델 우선 |
| same_model_429 | bounded failure | 승인 fallback 모델 성공·degraded 기록 |
| correlated_429 | bounded failure | 전체 모델이 막히면 bounded failure |
| primary_503 | 동일 모델 지역 전환 | 동일 모델 우선 |
| primary_timeout | 동일 모델 지역 전환 | 동일 모델 우선 |
| auth_401 | 한 번 시도 후 fail-fast | 타 모델·지역으로 권한 오류 은폐 금지 |
| headroom | 한 번 성공 후 공유 quota pool 선제 제한 | 요청 모델 pool 제한 후 승인 후보 |

HTTP와 SSE 모드를 각각 실행합니다.
`passed=true`는 이 기대 결과에 부합했다는 뜻이며 모든 요청 성공을 뜻하지 않습니다.
각 row에는 성공·실패 수, 실제 mock HTTP attempt 수, 최대 attempt, degraded 수와 model invariant가 있습니다.
첫 텍스트 이후 stream disconnect는 replay 없이 호출자에게 전파되는지도 별도 검사합니다.

```powershell
uv run python -m pytest -q
uv run python -m foundry_resiliency simulate --count 3 --output artifacts\simulation.json
uv run python -m foundry_resiliency simulate --stream --count 3 --output artifacts\streaming.json
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
az bicep build --file infra\main.bicep --outfile artifacts\main.json
```

## 실제 검증과 결과 저장

로컬 실측 결과는 [verification.json](verification.json)에 기록합니다.
모의 TTFT는 SDK·로컬 코드의 시간이며 모델·네트워크 성능 증거가 아닙니다.
실험별 상세 JSON은 `artifacts`에 저장하고 고객 데이터나 키를 넣지 않습니다.
pytest의 fake clock 테스트는 긴 Retry-After를 실제로 기다리지 않고 규칙을 검증합니다.
SSE 테스트는 HTTP chunk를 텍스트 토큰으로 잘못 계산하지 않습니다.

`smoke`는 `--confirm-paid` 없이는 호출하지 않습니다.
실제 endpoint가 없다면 live test는 `not_run`으로 남깁니다.
preflight는 quota Scope·사용 가능 capacity·전체 Agent 기능을 검증하지 않습니다.

## 실환경 합격 기준 수립

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
