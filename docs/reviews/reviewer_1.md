# Reviewer 1 독립 리뷰

## 검토 대상

- 논문: *State-Grounded Conversational Retrieval for Interactive Analytical Dashboards*
- 원고: [`paper/6a549455b0d9b08aa493b750/00_main.pdf`](../../paper/6a549455b0d9b08aa493b750/00_main.pdf)
- 심사 관점: SIGIR-AP

## 논문 요약

본 논문은 대시보드 조작 이후 발생하는 “And this one?” 같은 불완전한 질의를 대화 기록뿐 아니라 활성 차트, 필터, 계층, 상호작용 이력을 이용해 해결하는 *state-grounded conversational retrieval* 문제로 정식화한다. 이를 위해 차트·쿼리 결과와 분석적 범위 및 provenance를 결합한 Analytical Evidence Record(AER)를 제안한다.

## 강점

1. **실제적인 문제를 명확한 IR 문제로 정식화했다.**

   \(S_t=(D_t,A_t,Z_t)\), AER, `Text + Compat + Affinity + Recency` 구조를 통해 UI 동기화 문제를 세션 기반 근거 검색 문제로 설명한 점이 명료하다. Table 1의 대시보드 행동과 검색 신호 간 매핑도 설득력이 있다.

2. **검색 실패와 답변 생성 실패를 분리했다.**

   Figure 1 및 Section 5에서 AER 검색, 답변 생성, 브라우저 조작 실패를 서로 다른 평가 대상으로 구분한다. 특히 비교 조건이 맞지 않는 과거 state-aware trace를 Table 6과 직접 비교하지 않은 것은 연구적으로 신중하다.

3. **제한된 실험 범위에서는 결과가 일관적이다.**

   Table 3에서 30개 과제의 evidence coverage를 검증했고, Table 4에서는 R@1이 query BM25 0.3158, history BM25 0.4912, dialogue+state 0.9123으로 향상된다. Table 5도 개선이 explicit 질의보다 context-dependent·elliptical 질의에서 발생한다는 것을 잘 보여준다.

## 약점 및 개선안

1. **핵심 벤치마크가 합성 데이터에 지나치게 의존한다.**

   57개 instance는 19개 seed에 동일한 형태의 context-dependent 및 “And this one?” 질의를 붙인 결정론적 변형이다. 반면 state에는 정답과 밀접한 focused chart, active tab, filter가 포함된다. 따라서 Table 5는 자연스러운 문맥 복원보다 “정답을 가리키는 state를 활용할 수 있는가”에 가까운 sanity check일 가능성이 있다.

   **개선안:** 실제 사용자의 후속 질의와 상호작용 로그를 수집하고 여러 대시보드와 도메인을 포함해야 한다. stale state, 잘못된 focus, 유사 차트, 필터 해제, 주제 전환 및 대화-state 충돌을 hard negative로 추가하고 세션·대시보드 단위로 데이터셋을 분리해야 한다.

2. **검색 방법과 성능 원인에 대한 설명이 부족하다.**

   Eq. (3)의 가중치, 정규화, 동점 처리, linked-chart 구성, recency 및 invalidation 규칙이 재현 가능한 수준으로 제시되지 않는다. 또한 ablation이 없어 active tab, filter, focus 중 어떤 신호가 개선을 만드는지 알기 어렵다.

   **개선안:** 전체 ranking 절차와 정확한 feature 계산을 알고리즘 또는 의사코드로 제시해야 한다. 각 state feature의 ablation과 오류 분석을 추가하고, state-serialization BM25, query rewriting, dense/hybrid retrieval 및 LLM reranking과 비교하는 것이 필요하다.

3. **실제 end-to-end 효용을 입증하지 못했다.**

   Table 6의 dashboard-only 실험은 exact 8/30, timeout 15/30이지만 동일 조건의 state-aware 실행이 없다. \(N=5\) 사용자 연구 역시 과제 난이도와 사용 가능한 기능이 동시에 변경되어 state-aware 지원의 효과를 분리하기 어렵다. 특히 S2의 73.33%가 어떻게 산출되었는지도 명확하지 않다.

   **개선안:** 동일한 모델, 대시보드 snapshot, 프롬프트, step budget 및 timeout으로 no-state와 state-aware 조건을 paired 비교해야 한다. 사용자 연구에서는 동일 과제를 dashboard-only, chat-only, state-grounded 조건에서 counterbalance하고 정확도, 시간, 오류 및 workload를 비교해야 한다.

## 잠정 종합평가

- **평가:** Weak Reject (4/10)
- **확신도:** 4/5

문제 정식화와 AER 표현, 검색·생성·브라우저 실패를 분리한 관점은 흥미롭고 SIGIR-AP 주제에도 적합하다. 다만 현재 핵심 수치는 작은 결정론적 세션에서 얻은 state 활용 sanity check에 가깝고, 강한 검색 비교군, ablation 및 matched end-to-end 평가가 없어 주된 실증적 주장을 뒷받침하기에는 부족하다.
