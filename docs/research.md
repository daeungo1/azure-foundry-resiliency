---
title: 2026년 공식 근거와 참고 저장소 Top 3
description: 조사 기준, GitHub 인기도와 유지보수 상태, 채택 및 제외 근거
ms.date: 2026-10-21
---

## 선정 기준

2026-10-16 GitHub API와 Microsoft Learn을 조회했습니다.
공식성, 문제 적합성, Python 적용성, 2026년 유지보수 상태를 우선하고 stars를 보조 지표로 사용했습니다.
이는 전체 GitHub 인기 순위가 아닌 이 프로젝트에 대한 추천 순위입니다.
Stars와 HEAD는 계속 변경되므로 조사 시점 값과 commit을 함께 기록합니다.

| 순위 | 저장소                                                                 | Stars  | 확인한 HEAD 날짜 | 적용 관점                         |
|------|------------------------------------------------------------------------|--------|------------------|-----------------------------------|
| 1    | [Azure-Samples/AI-Gateway](https://github.com/Azure-Samples/AI-Gateway)    | 998    | 2026-09-16       | Microsoft 공식 APIM AI gateway lab |
| 2    | [BerriAI/litellm](https://github.com/BerriAI/litellm)                      | 61,347 | 2026-10-16       | Python routing·cooldown·fallback   |
| 3    | [openai/openai-python](https://github.com/openai/openai-python)            | 31,858 | 2026-10-16       | 공식 Python SDK, async·SSE·raw headers |

### Microsoft AI Gateway

고정 commit은
[`1679e3137d2fe7592d2a668d77ff71550d35b69e`](https://github.com/Azure-Samples/AI-Gateway/tree/1679e3137d2fe7592d2a668d77ff71550d35b69e)입니다.

* [Backend pool load balancing](https://github.com/Azure-Samples/AI-Gateway/tree/1679e3137d2fe7592d2a668d77ff71550d35b69e/labs/backend-pool-load-balancing)
* [Model routing](https://github.com/Azure-Samples/AI-Gateway/tree/1679e3137d2fe7592d2a668d77ff71550d35b69e/labs/model-routing)
* [Token rate limiting](https://github.com/Azure-Samples/AI-Gateway/tree/1679e3137d2fe7592d2a668d77ff71550d35b69e/labs/token-rate-limiting)

Python notebook, Bicep와 APIM policy를 함께 볼 수 있습니다.
운영 중앙 거버넌스의 참고안으로 채택합니다. 이 repo에서 APIM을 실제 배포한 것은 아닙니다.
라이선스는 MIT입니다.

### LiteLLM

고정 commit은
[`dba86bec28cb483c9937470c5d950f5e03699d3b`](https://github.com/BerriAI/litellm/tree/dba86bec28cb483c9937470c5d950f5e03699d3b)입니다.
[Routing 가이드](https://docs.litellm.ai/docs/routing)는 endpoint pooling, timeout, cooldown,
fallback과 Redis를 이용한 공유 상태를 설명합니다.

운영 gateway 대안으로 적합하지만 작은 reference lab에 gateway 전체를 의존시키지 않기 위해
패턴만 참고했습니다. 본 프로젝트의 런타임 dependency가 아닙니다.
기본 소스는 MIT이며 `enterprise/` 영역은 별도 라이선스입니다.
기업 기능을 무료·MIT 기능으로 가정하면 안 됩니다.

### OpenAI Python SDK

고정 commit은
[`325da45934ddb1c1b6a0bed1681a8cf46c9a8712`](https://github.com/openai/openai-python/tree/325da45934ddb1c1b6a0bed1681a8cf46c9a8712)입니다.
Microsoft repo는 아니지만 stars가 많고 이 프로젝트의 Python 호출 경로에 직접 적합한 공식 SDK입니다.

비동기 client, Responses streaming, raw response header 접근을 사용합니다.
라우터가 전체 retry budget을 소유하도록 `max_retries=0`을 강제합니다.
라이선스는 Apache-2.0입니다.
참고 HEAD와 실제 설치 SDK 버전은 다릅니다. 테스트 보고서에 설치 버전을 기록합니다.

### 제외한 후보

[Azure-Samples/azure-openai-apim-load-balancing](https://github.com/Azure-Samples/azure-openai-apim-load-balancing)은
35 stars이며 단순 round-robin과 3회 retry의 이해에 유용합니다.
하지만 조사 시점 마지막 코드 push가 2024-04-04이고, 현재의 Global Standard·구독 quota 통합·
명시적 혼합 모델 승인·TTFT 예산을 다루는 중심 reference로는 부족하여 Top 3에서 제외했습니다.
존재 여부를 확인할 수 없는 유사 이름의 저장소는 추천하지 않았습니다.

## 2026년 공식 문서에서 확인한 사항

| 항목 | 확인 사실 | 공식 근거 |
|------|-----------|-----------|
| 네 모델 | 모델 ID 및 위 README의 고정 버전이 Global Standard 지역 표에 존재 | [지역 가용성](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability#global-standard) |
| Global Standard | 리소스 리전과 별개로 추론은 Azure 글로벌 리전에 라우팅될 수 있음 | [Deployment types](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types) |
| 낮은 사용량의 429 | shared-pool 보호 조정, token 추정, burst 등으로 configured quota 미만에서도 가능 | [Quota와 429](https://learn.microsoft.com/azure/foundry/openai/how-to/quota#why-you-might-see-429s-even-when-token-usage-metrics-are-below-quota) |
| 2026 quota 통합 | 편입된 모델은 subscription/model/version 범위로 Global Standard 리전 간 quota 공유 | [Quota scope 확인](https://learn.microsoft.com/azure/foundry/foundry-models/quotas-limits#check-quota-management-scope) |
| 모델별 전환 상태 | Foundry Quota의 `Scope`가 Global/Data Zone이면 새 체계, 리전이면 기존 체계 | [Quota management](https://learn.microsoft.com/azure/foundry/foundry-models/quotas-limits) |
| 다중 구독 한계 | quota와 달리 usage-tier latency 기준은 tenant 전체 모델 소비량을 합산 | [Usage tiers](https://learn.microsoft.com/azure/foundry/openai/quotas-limits#usage-tiers) |
| TTFT 지표 | 절대 지연 진단은 `AzureOpenAITimeToResponse`, normalized TTFT와 혼동 금지 | [Latency](https://learn.microsoft.com/azure/foundry/openai/how-to/latency) |
| APIM circuit | Consumption 미지원, gateway instance 간 상태 동기화되지 않음 | [Backend pools와 circuit breaker](https://learn.microsoft.com/azure/api-management/backends) |
| 호출 권한 | 일반 관리 Contributor와 Entra 추론 DataActions를 구분 | [Azure OpenAI RBAC](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/role-based-access-control) |

고객 메일의 “대개 수 시간”과 과거 타 고객의 “약 1시간”은 복구 SLA가 아닙니다.
첫 토큰 지연 SLO나 복구 시간을 이 사례에서 도출하지 않습니다.

## Streaming TTFT 그룹 설계의 추가 근거

2026-10-21에 Microsoft Architecture Center, APIM 문서, AI-Gateway lab과 LiteLLM routing을 재확인했습니다.
AI-Gateway의 HEAD는 위 고정 commit과 동일했습니다. 위 Top 3의 stars는 최초 조사 시점 값으로 유지합니다.

| 참고 사례 | 확인한 내용 | 프로젝트 적용 |
|-----------|-------------|---------------|
| [Microsoft Architecture Center gateway 패턴](https://learn.microsoft.com/azure/architecture/ai-ml/guide/generative-ai-gateway-multiple-models) | 다중 구독·리전 backend, API 호환성, 보안 경계와 stateful session 고려 | 지역별 진입 경로와 안전성 조건을 먼저 정하고 모델 변경을 별도 관리 |
| [AI-Gateway backend pool lab README](https://github.com/Azure-Samples/AI-Gateway/blob/1679e3137d2fe7592d2a668d77ff71550d35b69e/labs/backend-pool-load-balancing/README.MD) | priority 1 우선, priority 2 동급 분산의 PTU fallback 예시 | pool·전환 패턴만 참고하며 이번 Global Standard BP에 PTU는 도입하지 않음 |
| [APIM backend 지원 기능](https://learn.microsoft.com/azure/api-management/backends) | weighted·priority pool, circuit·Retry-After, 상위 circuit 사용 불가 시 하위 그룹 진입 | TTFT 자동 학습이나 느린 응답만으로 하위 pool 진입을 보장한다고 설명하지 않음 |
| [LiteLLM routing 전략](https://docs.litellm.ai/docs/routing) | weighted pick 기본 권장, 선택적 latency window·lowest latency buffer | 동급 후보 분산을 참고하되 자체 SSE 첫 텍스트 계측·quota 보호와 함께 평가 |

안전성 조건으로 후보를 제한한 뒤 사용자·업무·모델별 실측 TTFT로 Primary와 Resilience를 구성하는 것은
위 사례를 조합한 프로젝트의 권장 설계입니다.
특정 리전 순위·p95 경계·관측 창·표본 수는 공식 공통 권장값으로 제시하지 않습니다.
일반 response time 기반 router가 첫 유효 텍스트를 기준으로 측정한다고 가정하지 않습니다.
실제 적용 절차와 현재 구현의 경계는 [TTFT BP](latency.md)에 있습니다.
