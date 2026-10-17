---
title: 고객 공유용 Global Standard 복원력 요약
description: PayGo 429 대응, 글로벌 Agent TTFT와 검증 범위에 대한 안내
ms.date: 2026-10-16
---

## 제안 요약

현재 운영 중인 KR, UK, US, CA Agent와 향후 유럽 확장을 고려하여
Global Standard(PayGo) 기반의 복원력 구성을 준비했습니다.
PTU로 전환하는 것을 전제로 하지 않습니다.

첫 단계에서는 동일 모델·버전의 여러 리전 endpoint 간 장애 전환을 적용하고,
다음 단계에서는 업무 품질·기능 호환성이 승인된 타 모델에 한해서 fallback을 허용합니다.
이 두 경우를 분리하여 모델 변경 여부를 명확히 기록합니다.

## 429에 대한 해석

설정된 quota 미만의 사용량에서도 공유 용량 풀의 일시적 압력, effective rate limit 조정,
요청 token 추정, burst 등의 이유로 429가 발생할 수 있습니다.
`x-ratelimit-remaining-requests`, `x-ratelimit-remaining-tokens`와 reset·Retry-After 정보를 활용하여
선제 admission, 제한된 재시도, circuit breaker와 승인된 후보 전환을 수행합니다.

2026년 도입된 quota 체계에 편입된 모델은 동일 구독의 모든 리전에서
같은 모델·버전의 Global Standard quota를 공유합니다.
따라서 멀티 리전만으로 quota가 늘어나거나 shared-pool throttling이 반드시 사라진다고
보장할 수 없습니다. 모델별 실제 quota `Scope`를 확인해야 합니다.

## TTFT 관리

사용자 인접 진입점과 지역별 Agent를 사용하고 불필요한 대륙 간 왕복을 줄입니다.
연결·인증 token 재사용, prompt 및 RAG 크기 관리, 지원되는 prompt caching,
스트리밍 전달과 짧은 bounded queue를 함께 적용합니다.
첫 텍스트 이전의 장애 전환은 시간 예산 안에서만 수행하며,
첫 텍스트 이후에는 자동 재생으로 중복 응답이나 tool 부작용을 만들지 않습니다.

Global Standard의 리소스 리전은 추론 처리 리전이나 일정한 TTFT를 보장하지 않습니다.
국가별 실제 Agent 경로에서 TTFT·TBT·전체 응답 시간·실패율을 함께 검증해야 합니다.

## 현재 검증 범위와 다음 단계

Python SDK를 이용한 로컬 모의 HTTP/SSE 장애 시나리오와 routing contract를 제공합니다.
실제 Azure의 capacity, global latency, 고객 Agent의 end-to-end 성능을 검증한 것은 아닙니다.
실환경에서는 전용 test deployment와 비용·트래픽 상한을 정한 뒤 소량 검증부터 진행합니다.
과거의 특정 고객 장애 지속 시간을 복구 SLA로 제시하지 않습니다.

다중 구독은 향후 확장 참고안으로만 유지합니다.
추후 진행하면 별도 승인 quota와 운영 identity 권한을 확인합니다.
다중 구독도 독립된 글로벌 용량 풀이나 낮은 TTFT를 보장하는 것은 아닙니다.
