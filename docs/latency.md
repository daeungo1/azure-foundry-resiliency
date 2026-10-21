---
title: Streaming TTFT 기반 글로벌 Agent 라우팅 BP
description: 안전성 조건, 실측 TTFT 그룹, 호출 시간 예산과 지역별 검증 절차
ms.date: 2026-10-21
---

## 권장 접근

리전 이름으로 고정된 그룹을 만들기보다 **안전성 조건 → 실측 TTFT 그룹 → 가용 예산** 순서로 호출 대상을 정합니다.
사용자 인접 Agent/gateway를 배치하고, 같은 모델·버전의 유사 성능 후보들에 트래픽을 분산합니다.
endpoint 장애나 quota 압력에는 남은 TTFT 예산 안에서 다른 정상 후보로 전환합니다.

| 접근 | 적합한 단계 | 장점과 한계 |
|------|-------------|-------------|
| 지리적 근접성 기반 정적 순서 | 실측 전 초기 배치 | 단순하지만 글로벌 추론·queue·모델 부하를 반영하지 못함 |
| 실측 TTFT profile과 상태 기반 전환 | 운영 적용의 권장 시작점 | 주기적으로 승인한 후보 순서를 갱신하면서 admission·circuit으로 보호 |
| 요청별 동적 latency routing | 충분한 관측과 제어가 확보된 이후 | 부하 변화에 반응하지만 쏠림·순위 진동·cache 손실과 제어 계층 운영 필요 |

이 설계는 [공식 BP와 참고 구현](#공식-bp와-참고-구현)의 패턴을 조합한 프로젝트 권장안입니다.
Microsoft가 특정 리전 순위, 그룹 경계 또는 네 모델의 TTFT 목표를 보장한 것은 아닙니다.

![안전성부터 TTFT 그룹 및 복구까지 6가지 호출 방안](diagrams/ttft-routing-strategy.svg)

그림의 1~6은 [README 요약 표와 번호별 실행 기준](../README.md#tldr-핵심-방안-6가지)에 대응합니다.
Primary·Resilience의 지역 별칭은 그룹 구성 예시이며 실측 성능 순위가 아닙니다.
주요 실행 기준과 [전후 TTFT·성공률 비교](../README.md#적용-전후-ttft와-응답-성공률)는 README에서 함께 볼 수 있습니다.
새 `compare` 명령은 통제된 지연의 로컬 SSE 비교이며 실제 Azure endpoint 순위를 정하는 도구는 아닙니다.

## 무엇을 측정할 것인가

사용자가 보는 첫 유용한 토큰의 시간은 다음을 모두 포함합니다.

```text
User TTFT =
  사용자 -> Edge/지역 Agent 네트워크
  + 인증/검색/도구/프롬프트 준비
  + admission 대기와 gateway
  + Foundry 왕복/대기/추론 첫 텍스트 생성
  + 사용자까지의 stream 전달
```

CLI의 `first_token_ms`는 Python 라우터 호출 시작부터 SDK의 첫 non-empty text delta까지입니다.
이 값은 최종 성공 attempt만의 시간이 아니라 앞선 실패·대기·전환 시간도 포함합니다.
개별 endpoint 순위에는 별도로 attempt 전송부터 첫 텍스트까지의 관측값을 사용하고,
최종 SLO에는 사용자 요청 시작부터 첫 유용한 텍스트까지의 시간을 사용합니다.
국가별 사용자 last-mile이나 실제 Agent 검색·tool latency를 포함하지 않습니다.
Global Standard의 리소스를 사용자 가까이에 놓아도 실제 추론 리전과 TTFT는 보장되지 않습니다.
`simulate` 결과는 별도입니다. 모의 정책 시계를 사용하므로 결과의 `first_token_ms`를
실환경 TTFT와 비교하면 안 됩니다. 실제 TTFT 임계치 평가는 `smoke --stream --ttft-budget-ms`로
승인된 실제 배포에 대해 수행해야 하며 현재 공유 테스트에서는 실행하지 않았습니다.

Streaming 요청은 `stream=True`로 보내고 첫 non-empty `response.output_text.delta`를 측정합니다.
HTTP 200, 첫 byte, `response.created`, 빈 delta와 keepalive는 첫 유효 텍스트가 아닙니다.
content safety 처리로 전달을 보류하는 경우에는 사용자에게 허용된 텍스트가 실제 전달된 시점도 따로 측정합니다.
TTFT를 낮추기 위해 안전성 검사를 생략하거나 스트림 buffering 정책을 무조건 해제하지 않습니다.

Azure Monitor에서는 절대 TTFT에 가까운 `AzureOpenAITimeToResponse`를 사용합니다.
`AzureOpenAINormalizedTTFTInMS`는 prompt token 수로 정규화된 지표이므로
절대 TTFT 목표와 비교하지 않습니다. TBT와 TTLT도 함께 봐야 합니다.

## 리전보다 먼저 정할 그룹 기준

| 분류 축 | 구분할 정보 | 적용 이유 |
|---------|-------------|-----------|
| 정책 도메인 | tenant, 허용 데이터 처리 범위, identity·네트워크, content safety | 속도와 무관하게 위반 후보 제외 |
| 모델 계약 | 모델·고정 버전, context, API·출력 schema, 필요한 기능 | 동일 모델 전환과 타 모델 품질 변경을 분리 |
| 사용자 접점 | Region01~RegionN의 실제 사용자·Agent/gateway 경로 | 개발자 PC의 시장 옵션 변경과 글로벌 측정 구분 |
| 업무 유형 | 짧은 대화, 긴 추론, RAG·tool 단계, 입력 token 구간 | 다른 정상 TTFT 분포를 한 순위에 섞지 않음 |
| 실행 조건 | connection·prompt cache warm/cold, 동시성·시간대 | 실제 운영 부하와 cache locality 반영 |
| 용량·장애 범위 | 실제 quota Scope·pool, 구독, endpoint circuit | 빠르지만 전송 예산이 없는 후보로의 집중 방지 |

모델 네 개를 하나의 latency 순위로 합치지 않습니다.
각 모델·버전과 업무별로 먼저 순위를 만들고, 평가를 통과한 다른 모델은 별도의 fallback 그룹으로 둡니다.
리전·구독은 후보의 속성이며 어느 한 구독 전체를 빠른 그룹으로 간주하지 않습니다.
Global Standard가 데이터 처리 제한에 맞지 않으면 endpoint의 리전 필터만으로 해결하지 않습니다.
적합한 배포 유형과 모델 가용성을 먼저 재검토합니다.

## Primary와 Resilience 그룹 구성

| 그룹 | 편입 조건 | 선택·전환 정책 |
|------|-----------|----------------|
| Primary | 동일 모델·버전, 충분하고 최신인 표본, 업무 p95 TTFT·목표 내 응답률 충족 | 최저 지연과 유사한 후보를 함께 두고 headroom·동시성 기준으로 분산 |
| Resilience | 동일 계약의 다른 승인 endpoint, 장애 시 남은 예산에 맞는 TTFT | Primary의 429·일시 오류·첫 텍스트 timeout 시 검토 |
| Approved fallback | 별도 평가·승인한 모델, 정책·기능·시간 예산 충족 | 허용된 전환 방향과 요청 opt-in이 모두 있어야 사용 |
| 검증 대기 | 신규 후보, 부족하거나 만료된 표본 | 검증된 빠른 후보로 취급하지 않고 제한된 probe·canary로 평가 |

예를 들어 Region01 사용자의 같은 업무에서 Subscription01/Region01과 Subscription02/Region03이
동급의 정상 TTFT로 측정되면 두 endpoint를 Primary에 함께 둘 수 있습니다.
Region02 사용자의 순위는 다를 수 있으므로 별도로 측정합니다.
이는 그룹 편성 방법의 예이지 해당 리전의 실측 순위가 아닙니다.
현재 공개 예제의 Region01~Region04 선호도도 실측 ranking이 아니며,
`--include-expansion`으로 추가하는 Region05 역시 편입 전 검증합니다.

같은 quota pool의 잔여 예산이 없으면 같은 그룹의 다른 리전도 그 예산을 재사용할 수 없습니다.
별도 승인 quota pool의 후보를 활용하되 구독·리전별 물리 capacity 독립성을 가정하지 않습니다.
지역 preference는 데이터 거버넌스 allowlist를 대신하지 않습니다.

### 순위 갱신과 쏠림 방지

* 성공 요청의 p50/p95/p99와 함께 전체 요청 중 TTFT 목표 내 유효 응답 비율, 429·timeout·실패율을 봅니다.
  안전성 거절은 별도 분류하고, 거절을 없애기 위해 다른 모델을 재호출하지 않습니다.
* 통계의 집계 창·표본 수·측정 시각을 보관합니다. 표본 부족·만료·관측 장애 시에는
  최근 승인한 정적 순서로 복귀하고 상태를 알립니다. 예산·권한·안전성 검사는 유지합니다.
* 가장 빠른 한 후보에 모든 요청을 몰지 않습니다. 유사 지연 대역과 가중치를 정하고
  각 endpoint 및 공유 quota pool의 동시성·요청·토큰 예산을 함께 적용합니다.
* 승격에는 연속 관측과 최소 유지시간을 적용하여 순위 진동을 줄입니다.
  반면 실제 429·연결 장애는 latency 통계 갱신을 기다리지 않고 cooldown·circuit으로 처리합니다.
* 신규·복구 후보는 제한된 probe로 확인하고 트래픽을 점진적으로 복귀시킵니다.
  표본 수·대역 폭·갱신 주기는 고객 부하로 검증할 설정이며 Microsoft 공통 권장 상수가 아닙니다.

### 현재 구현과 운영 플랫폼 연결

| 기능 | 현재 상태 | 운영 적용 |
|------|-----------|-----------|
| Streaming 첫 텍스트 측정·첫 텍스트 전 전환 | Python SDK 경로에 구현 | 실제 Agent 경로의 사용자 TTFT 추가 계측 |
| 헤더 admission·동시성·cooldown·circuit | 단일 프로세스에 구현 | 여러 replica에는 공유 예산·공정성 적용 |
| 후보 순서 | 모델 → 정적 지역 선호도 → priority → in-flight·사용 순서 | 실측 후 운영자가 승인한 순서부터 반영 |
| TTFT 통계·자동 그룹 갱신·성능 가중 분산 | 미구현, 권장 운영 설계 | 관측·제어 계층을 추가하고 수용 시험 후 활성화 |
| 정책 도메인·content safety 일치 검사 | 범용 자동 필터 미구현 | 승인한 설정만 배포하고 gateway·application에서 검증 |

현재 설정의 `region_preferences`는 리전 순서이고 backend별 실측 profile 형식이 아닙니다.
시장·업무별 endpoint 순서나 latency 그룹을 전부 표현할 수 있다고 가정하지 않습니다.
현재 router는 요청 모델 후보 다음에 fallback을 검토하며,
TTFT 예측을 이유로 정상 동일 모델 후보를 건너뛰는 별도 정책도 구현하지 않습니다.
이를 추가할 때는 모델 유지 정책과 사용자 시간 예산의 우선순위를 명시적으로 승인받습니다.

## TTFT를 낮추는 실행 포인트

1. 사용자 인접 Edge/Agent/gateway를 사용하고 불필요한 대륙 간 hop을 제거합니다.
   gateway를 Region03 한 곳에 두고 Region01 사용자가 항상 왕복하게 만들지 않습니다.
2. SDK client와 HTTPS connection pool을 재사용합니다. Managed Identity token도 SDK의
   credential cache를 사용하고 매 요청 CLI 로그인이나 client 재생성을 하지 않습니다.
   Responses SDK resource의 초기화도 서비스 readiness 전에 완료하고, 초기화 비용을 포함한 cold-start는 별도 측정합니다.
   SDK 선로딩이 모델·prompt cache의 warm 상태를 보장하는 것은 아닙니다.
3. streaming을 켜고 안전성 전달 정책을 유지하면서 proxy/ingress/클라이언트의 불필요한 response buffering을 끕니다.
   SSE keepalive와 idle timeout을 점검하고 완료 후 한 번에 출력하지 않습니다.
4. 긴 system prompt, 대화 이력, RAG 문서를 줄입니다. 필요한 데이터만 검색하고
   서로 독립적인 검색·조회는 병렬화합니다. 요약 때문에 필요한 사실을 잃지 않도록 평가합니다.
5. 재사용되는 정적 prompt prefix를 앞에 안정적으로 배치하여 지원되는 prompt caching을 활용합니다.
   지역·모델 전환에서 cache locality가 사라질 수 있으므로 warm/cold를 나눠 측정합니다.
6. 업무 품질을 만족하는 모델과 지원 parameter를 선택합니다. 모델별 지원이 확인된 경우에만
   reasoning effort 등 지연 관련 옵션을 조정합니다. 이 text adapter는 해당 옵션을 임의 전송하지 않습니다.
7. 큰 output 상한을 불필요하게 요청하지 않습니다. 이는 quota 선점과 TTLT에 영향을 주며,
   output 상한 축소만으로 TTFT가 반드시 줄어드는 것은 아닙니다.
8. 동시성·대기열을 제한하여 긴 queue를 만들지 않습니다. 부하를 천천히 올리고
   429, headroom, TTFT, TBT, 오류와 비용을 함께 봅니다.
9. 재시도는 짧고 제한된 budget 내에서만 합니다. 한 endpoint에서 긴 backoff로 기다리기보다
   즉시 사용할 수 있는 승인 후보를 선택합니다. server Retry-After를 무시하지 않습니다.
10. 동일 요청을 여러 지역에 무조건 복제하는 hedging은 기본 비활성화합니다.
    중복 과금, quota 소모, 서버 잔여 실행, Agent tool 중복 부작용이 있습니다.

## 시간 예산

사용자 목표에서 이미 경과한 시간과 전달 여유를 제외해 다음 호출의 예산을 정합니다.

```text
남은 호출 예산 = 사용자 TTFT 목표 - 요청 시작 후 실제 경과 시간 - 사용자 전달 여유
```

각 attempt의 예상 첫 텍스트 시간과 admission 대기가 이 예산 안에 들어오는지 판단하고,
실제 deadline을 최종 상한으로 적용합니다. 예측 percentile은 개별 요청의 보장이 아닙니다.
인증·RAG·gateway·모델 각각의 p95를 더한 값을 사용자 p95로 취급하지 않습니다.
최종 SLO는 같은 요청의 end-to-end trace로 검증합니다.
Resilience나 fallback까지 필요한 시간을 남기지 못하면 모든 후보를 순회하지 않고 명시적으로 종료합니다.
사용자에게 안내문이나 keepalive를 먼저 보낸 시간을 모델의 첫 유효 응답으로 기록하지 않습니다.

예제의 첫 응답 deadline은 8초, stream attempt의 첫 텍스트 제한은 2초입니다.
전체 스트림 context에는 별도의 60초 제한이 있습니다.
이 값들은 구성 시연용이며 고객 SLO나 서비스 성능 보장이 아닙니다.

2초 first-token budget이 모델 특성보다 짧으면 멀쩡한 backend를 계속 timeout 처리할 수 있습니다.
특히 복잡한 추론 업무는 단순 짧은 응답과 다른 기준을 사용해야 합니다.
업무군별 목표를 정한 뒤 국가별 warm/cold baseline을 보고 예산을 정합니다.
마지막 fallback에 남은 시간이 없으면 추가 요청을 보내지 않습니다.
한 번 첫 텍스트를 노출한 stream은 재시도하지 않습니다.

## 실제 측정 절차

1. Region01~Region04의 실제 Agent hosting 위치와 사용자 접점에 probe를 둡니다.
   개발자 PC 한 대의 `--market` 변경은 국가별 측정이 아닙니다.
2. 동일 prompt 길이, output 예산, 모델·버전과 동시성으로 비교합니다.
   cold-start, warm connection, prompt-cache warm/cold를 나눕니다.
3. 아래 Streaming smoke로 경로를 점검한 뒤, 전용 harness에서 endpoint를 하나씩 고정하여 비교합니다.
   고정 검증은 별도 test profile로 수행하며 일반 운영의 자동 전환 결과와 섞지 않습니다.
4. 국가·ISP·업무별로 시간대를 나눠 충분한 표본을 모으고, 표본 수와 관측 기간을 결과에 포함합니다.
   기존 CLI는 시장별 endpoint TTFT matrix나 자동 그룹을 생성하는 도구가 아닙니다.
5. 정상, 특정 endpoint 오류, quota 압력, 승인 fallback을 나눠 결과를 비교합니다.
   사용자 TTFT 목표 내 유효 응답률·실패율·시도 증폭·비용·품질을 기존 순서와 비교하고 canary로 적용합니다.
6. 실제 Global Standard capacity를 일부러 고갈시키지 않습니다.
   모의 fault와 실제 service 429를 보고서에서 분리합니다.

```powershell
uv run python -m foundry_resiliency smoke --config configs\single-subscription.local.json `
  --model gpt-6-luna --market Region02 --stream --count 10 --confirm-paid `
  --ttft-budget-ms 2000 --output artifacts\live-region02-luna.json
```

`2000`은 사용자가 지정한 실험 임계치의 예이며 보장값이 아닙니다.
성공 stream의 관측 p95가 임계치를 초과하거나 요청이 실패하면 exit code가 1입니다.
표본 10개의 p95는 거의 최대값인 기술 통계일 뿐 안정적인 production percentile 추정이 아닙니다.
실패 요청을 제거한 성공 TTFT만 보면 생존자 편향이 생기므로 실패율과 deadline 위반을 반드시 함께 보고합니다.
현재 CLI는 위 목표 내 응답률·업무별 cohort 집계까지 자동 산출하지 않으므로 운영 측정 harness에서 추가합니다.

## 다중 구독의 영향

별도 구독 quota는 admission 여유를 줄 수 있으나 inference 성능을 예약하지 않습니다.
같은 tenant의 usage-tier latency 기준은 모든 구독·리전의 모델 소비량을 합산할 수 있습니다.
따라서 “구독 2개면 TTFT도 절반” 또는 “동일 모델 capacity가 완전히 독립”이라고 설명하면 안 됩니다.
[공식 latency 가이드](https://learn.microsoft.com/azure/foundry/openai/how-to/latency)와
[usage-tier 기준](https://learn.microsoft.com/azure/foundry/openai/quotas-limits#usage-tiers)을 함께 확인합니다.

## 공식 BP와 참고 구현

2026-10-21에 다음 근거를 재확인했습니다. 공식 지원 기능과 프로젝트의 조합 설계를 구분합니다.

| 근거 | 채택할 패턴 | 그대로 적용하지 않을 부분 |
|------|-------------|----------------------------|
| [Microsoft Architecture Center의 다중 backend gateway](https://learn.microsoft.com/azure/architecture/ai-ml/guide/generative-ai-gateway-multiple-models) | 지역별 gateway, 보안 경계, API 호환성, 필요한 세션 상태 관리 | gateway 추가 자체가 지연 개선을 보장하지 않음 |
| [Microsoft AI-Gateway backend pool lab](https://github.com/Azure-Samples/AI-Gateway/tree/1679e3137d2fe7592d2a668d77ff71550d35b69e/labs/backend-pool-load-balancing) | priority group, 동급 후보 분산, 429·503 전환 | 원본은 PTU 우선 예시이므로 PTU 구성·retry 값을 복제하지 않음 |
| [APIM backend pool·circuit](https://learn.microsoft.com/azure/api-management/backends) | weighted·priority pool, Retry-After와 circuit | TTFT 자동 학습 기능이 아니며 gateway 간 상태도 전역 동기화되지 않음 |
| [LiteLLM routing](https://docs.litellm.ai/docs/routing) | latency 관측 창, 최저 지연에 가까운 후보군과 공유 사용량 상태 | 기본 권장은 weighted pick이며, latency 전략의 값도 첫 유효 텍스트 TTFT인지 별도 확인 |
| [Foundry latency](https://learn.microsoft.com/azure/foundry/openai/how-to/latency) | TTFT·TBT·토큰·부하를 함께 관측 | 정규화 TTFT를 절대 사용자 TTFT 목표와 비교하지 않음 |

APIM의 기본 priority pool은 상위 그룹의 모든 backend가 circuit으로 사용 불가일 때 하위 그룹을 사용합니다.
단순 TTFT 악화나 application의 시간 예산 때문에 다음 그룹으로 이동하는 정책은 별도 라우팅 제어가 필요합니다.
SSE를 전달할 때는 [forward-request 정책](https://learn.microsoft.com/azure/api-management/forward-request-policy)의
`buffer-response="false"` 구성을 검증하고,
사용자에게 텍스트가 전달된 뒤 APIM·application·SDK 어느 계층도 자동 replay하지 않도록 retry 소유자를 하나로 정합니다.
content safety 검사를 위한 의도적인 buffering은 별도 승인 정책으로 유지합니다.
