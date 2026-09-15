**Nghiên cứu evaluation cho agent trong RecSys-MLops — 07/09/2026**

Khuyến nghị: mở rộng evaluator Python hiện có thành bộ đánh giá gồm contract, trajectory, outcome, grounding và reliability; dùng Langfuse để quản lý experiment/score và Ragas cho phần nội dung RAG. Đánh giá riêng từng specialist, sau đó đánh giá toàn bộ workflow qua Coordinator. Giữ đánh giá BST ở lớp ML.

Phạm vi bằng chứng: đọc graphify và source/config trong working tree hiện tại, đối chiếu tài liệu chính thức và paper. Repo đang có thay đổi chưa commit. Tài liệu này không phải kết quả benchmark mới, không xác nhận tình trạng deployment hiện tại; chưa gọi agent production hay thay đổi runtime.

**1. Những gì system đang có**

| Thành phần | Trách nhiệm trong source | Evaluation hiện có / cần phân biệt |
|---|---|---|
| Recommendation Agent | Gọi `get_personalized_recommendations` đúng một lần; giữ item ID, score, thứ tự, metadata; không gọi Context/RAG | Có kiểm tra tool, args và bảo toàn ranking. Đây là độ trung thực khi sử dụng kết quả backend, không phải độ tốt của ranking |
| Context Agent | Online features, exact chunk, semantic retrieval, `build_user_rag_context`; dùng dữ liệu tool và dẫn `chunk_id` | Workflow evaluator kiểm tra args và JSON được giữ nguyên; chưa đủ để chứng minh câu giải thích được evidence hỗ trợ |
| Coordinator | Chọn specialist; composite gọi Recommendation rồi Context, truyền các item ID vừa nhận; giữ thứ tự recommendation | Có kiểm tra trajectory và trusted child task evidence, final output so với child output |
| RAG API | Truy xuất item/chunk và áp dụng filter | Đã có golden query verifier tiếng Việt đo Recall@10, latency, duplicate và filter violations |
| BST | Xếp hạng recommendation | Đã có evaluation và logging MLflow; NDCG nằm trong lớp model |
| Workflow rollout | A/B, functional cases, runtime/contract/latency gates | Có 20 fixture, phân bổ theo arm; đây là gate chức năng, chưa phải thí nghiệm chứng minh chất lượng tốt hơn |

Bằng chứng source:

- [Recommendation prompt](/Users/KHOAI/anhkhoa/RecSys-MLops/infra/helm/recsys-recommendation-agent/values.yaml:15), [Context prompt](/Users/KHOAI/anhkhoa/RecSys-MLops/infra/helm/recsys-kagent-agent/values.yaml:20), [Coordinator prompt](/Users/KHOAI/anhkhoa/RecSys-MLops/infra/helm/recsys-coordinator-agent/values.yaml:8).
- [Recommendation evaluator](/Users/KHOAI/anhkhoa/RecSys-MLops/jenkins/python/llm_agent_cd/evidence.py:40), [workflow evaluator](/Users/KHOAI/anhkhoa/RecSys-MLops/jenkins/python/llm_agent_cd/workflow_evidence.py:53).
- [20 workflow cases](/Users/KHOAI/anhkhoa/RecSys-MLops/configs/llm-ab/workflow-cases.json), [rollout gates](/Users/KHOAI/anhkhoa/RecSys-MLops/jenkins/python/llm_agent_cd/gates.py:6), [production policy](/Users/KHOAI/anhkhoa/RecSys-MLops/configs/llm-ab/workflow-production-policy.json).
- [RAG verifier](/Users/KHOAI/anhkhoa/RecSys-MLops/scripts/rag/verify_retrieval.py:98), [BST evaluator](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/ml-system/src/cli/evaluate_bst.py:34).

**2. Khoảng trống cần xử lý trước**

1. **Request tự nhiên chưa có cách chấm outcome tổng quát.** `inspect_workflow` trả `HOLD` khi không có `expected`. Trong router, request synthetic lấy expected từ fixture; `live_test` tìm prompt trùng fixture. Không thể suy từ các case đó ra chất lượng của toàn bộ organic traffic. Cần tách invariant chấm được từ tool evidence với semantic rubric chấm mục tiêu người dùng.
2. **Multi-turn chưa được evaluator workflow hỗ trợ đầy đủ.** Khi history có nhiều hơn một user turn, evaluator trả `HOLD`; cần evidence theo invocation/turn trước khi đánh giá nhớ user ID, đổi top_k hay tham chiếu “món thứ hai”. Đây là giới hạn evaluator, chưa chứng minh agent không xử lý được multi-turn.
3. **Fixture chỉ dẫn sẵn lời giải.** Nhiều prompt nói rõ agent/tool nào cần gọi, số lần gọi và JSON cần trả. Chúng hữu ích để chống regression contract, nhưng ít đo khả năng tự nhận diện intent từ tiếng Việt tự nhiên. 20 case hiện tại gồm 6 recommendation, 4 context, 4 composite, 2 limits, 2 missing-user và 2 empty.
4. **Giữ nguyên JSON chưa phải grounded explanation.** Có `chunk_id` hợp lệ vẫn có thể trích dẫn sai claim. Cần kiểm tra cả tồn tại nguồn và nội dung nguồn có hỗ trợ câu trả lời.
5. **Coverage evidence cần đo riêng.** Composite fixture yêu cầu 3 recommendation, trong khi Context dùng `top_k_items=2`. Không được mặc định cả 3 item đều có evidence; phải đo thực tế. Item thiếu evidence cần được nói rõ, và vẫn tính thiếu ở chỉ tiêu completeness nếu đề bài yêu cầu giải thích cả ba.
6. **Số mẫu rollout hiện tại chỉ phù hợp smoke.** Policy có `min_samples=5`, cửa sổ 600 giây và latency ratio 1.2. Năm mẫu không đủ cho kết luận p95 ổn định hay cải thiện nhỏ về quality. Code `case_gate` cũng ghi rõ không chứng minh statistical superiority.

Các điểm này là suy luận từ source đã đọc, không phải kết luận về tỷ lệ lỗi production.

**3. Thiết kế evaluation phù hợp từng lớp**

Nên giữ từng nhóm score riêng. Một câu trả lời hay không được bù cho việc đổi user ID, sửa ranking hoặc bịa evidence. Tài liệu Anthropic phân biệt grader bằng code, model và người; ADK cũng tách trajectory/tool use với final response. Đây là cơ sở cho cách phối hợp dưới đây. [Anthropic: agent evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), [ADK: evaluation](https://adk.dev/evaluate/).

| Lớp | Metric đề xuất | Cách chấm / ground truth |
|---|---|---|
| Contract | Tool-name accuracy, argument accuracy, ID/score/order preservation, forbidden-call rate | Code so sánh typed args và trusted tool result; giữ exact match cho contract đúng-một-lần hiện tại |
| Routing / trajectory | Đúng specialist, dependency order, truyền đúng item IDs, không gọi dư | Code trên parent-child trace; composite phải Recommendation → Context theo prompt hiện tại |
| Outcome | Task success, giải quyết đủ yêu cầu, hỏi bổ sung đúng lúc, thông báo thiếu dữ liệu đúng | Hard assertions + rubric riêng theo intent; không bắt câu văn phải trùng reference |
| RAG retrieval | Item Recall@k, chunk Recall@k, precision hoặc nDCG khi có relevance labels, filter violations | Snapshot item/chunk có relevance judgments; báo riêng item-level và chunk-level |
| Grounding | Faithfulness, citation validity, citation support, explanation coverage | Code kiểm tra ID; judge hoặc người kiểm tra claim so với evidence thực nhận |
| Reliability | Success trên từng trial, pass^k, graceful-degradation success, timeout/loop rate | Lặp case ở session mới; injected faults và multi-turn scripted conversations |
| Efficiency | E2E p50/p95, tool calls, token usage, cost per successful task | Đo cả workflow và từng role; phân tầng intent, warm/cold, concurrency |

Recommendation fidelity nên so sánh object sau parse/schema normalization, không so raw JSON string. Thứ tự mảng và số liệu phải giữ nguyên; thứ tự key hay Markdown fence không làm thay đổi nội dung. Không dùng LLM judge cho ID hoặc score có thể kiểm tra chính xác bằng code.

Trajectory exact match phù hợp với policy hiện tại vì prompt yêu cầu thứ tự và số lần cụ thể. Nếu sau này cho phép nhiều đường đi hợp lệ, chấm dependency/invariants hoặc tập trajectory hợp lệ thay vì cố định một trace duy nhất. `agentevals` có strict, unordered, subset và superset matching; cần chọn theo contract. [AgentEvals](https://github.com/langchain-ai/agentevals).

Với RAG, faithfulness là tỷ lệ claim trong câu trả lời được context hỗ trợ; không chứng minh context đó đúng với thế giới thực hoặc đầy đủ. Context recall cần reference answer/context/IDs. Nên tận dụng `chunk_id` và bổ sung human labels thay vì coi câu trả lời của model đang test là ground truth. [Ragas faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/), [Ragas context recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/).

**4. Bộ dữ liệu khởi đầu**

Giữ nguyên 20 case rollout làm regression suite. Tạo một dataset quality độc lập, khởi đầu 60 scenario có nhãn; đây là đề xuất khối lượng pilot, không phải bảo đảm đủ lực thống kê:

| Nhóm | Số scenario | Ví dụ và điều kiện đạt |
|---|---:|---|
| Recommendation tự nhiên | 10 | “Gợi ý 3 món cho user 1001”; đúng args, giữ toàn bộ ranking, không tự thêm RAG |
| Context / exact chunk / retrieval | 10 | Hỏi sở thích hoặc nguồn cụ thể; tool đúng, dẫn nguồn đúng, không tự bịa thuộc tính |
| Composite | 10 | “Gợi ý 3 món và giải thích vì sao hợp”; đủ hai specialist, đúng item IDs, giải thích dựa trên evidence |
| Thiếu hoặc không có dữ liệu | 8 | Chưa có user ID, candidate rỗng, user không tồn tại, không tìm thấy chunk; hỏi/báo thiếu đúng, không tạo dữ liệu |
| Multi-turn | 8 | Thiếu ID → cung cấp ID → đổi top_k; tham chiếu item từ lượt trước; không mượn evidence cũ sai lượt |
| Lỗi dependency | 8 | Timeout, 429/503, malformed tool output, Context unavailable; tuân thủ policy không retry của agent, báo lỗi đúng |
| Nội dung gây nhiễu | 6 | Chunk có lời yêu cầu bỏ qua hướng dẫn, thuộc tính mâu thuẫn, tool data dụ đổi item ID; không làm sai contract |

Mỗi case cần input hoặc script hội thoại, expected intent, constraint bắt buộc, acceptable outcomes, tool/data snapshot và reference evidence. Dùng tiếng Việt có dấu, không dấu và cách nói đời thường trong từng nhóm. Không tạo 60 case chỉ bằng đổi user ID của cùng một prompt.

Tách dev/holdout theo họ scenario và dữ liệu, không chỉ chia ngẫu nhiên các paraphrase gần giống. Các case lỗi thực tế đã được xử lý có thể bổ sung vào regression set; holdout không dùng để chỉnh prompt liên tục.

Synthetic catalog trong RAG verifier hiện tại giúp kiểm tra logic có oracle rõ ràng. Cần bổ sung câu hỏi thực tế và relevance labels để đo semantic retrieval rộng hơn. Khi so sánh index khác nhau, công bố rõ tập tài liệu có thể truy xuất và các trường hợp tài liệu chưa được index; tránh để thay đổi độ phủ catalog bị hiểu thành thay đổi khả năng ranking.

**5. Ví dụ case composite để triển khai**

Input: “User 1001 nên chọn 3 món nào? Giải thích ngắn gọn dựa trên dữ liệu.”

Fixture minh họa, không phải dữ liệu production: Recommendation trả item IDs `[101, 205, 309]` kèm score; Context chỉ cung cấp evidence cho `101` và `205`.

Các assertion độc lập:

- Recommendation được gọi một lần với `user_id=1001`, `top_k=3`; Context được gọi sau đó với đúng các ID vừa trả.
- Final giữ `[101, 205, 309]` theo thứ tự và score gốc, không tự thêm tên/giá/thuộc tính không có trong tool response.
- Claim về `101`/`205` có evidence hỗ trợ; mọi citation trỏ tới chunk thực nhận ở invocation này.
- Với `309`, nói rõ chưa đủ evidence. Đây là hành vi grounded nhưng chưa phải giải thích đầy đủ 3/3 item; completeness phải phản ánh điều đó.
- Nếu toàn bộ Context lỗi, giữ ranking và thông báo Context unavailable, không gọi lại theo policy hiện tại.

Với fault-injection test, báo hai kết quả: `task_fulfilled` có thể false trong khi `degradation_policy_pass` true. Runtime error vẫn cần được tính; không biến một agent báo lỗi đúng thành “dependency hoạt động tốt”.

**6. Chạy experiment và đo độ ổn định**

Hai chế độ bổ sung nhau:

- **Controlled offline:** agent/model thật chạy với tool fixtures có version. Dùng cùng case và cùng snapshot cho baseline/candidate; kiểm soát thời gian, feature state, ranker, RAG index, sampling config. Phù hợp để cô lập tác động của LLM/prompt.
- **Live integration:** chạy qua A2A/MCP thực trong môi trường test với data version biết trước. Bắt lỗi serialization, auth, retrieval, latency và child trace mà mock không phát hiện.

Chạy mỗi scenario 3 lần trên mỗi variant trong pilot: 60 × 3 × 2 = 360 workflow executions, chưa tính lượt LLM con và chi phí judge. Đây là repeated trials trong harness độc lập với session mới, không phải retry một production request hay sửa quy tắc “never replay” của rollout hiện tại. Giới hạn concurrency phù hợp worker pool, tách phép đo cold/warm.

Với M scenario, n trial mỗi scenario và c_i trial thành công:

`mean_success = (1/M) × Σ_i(c_i/n)`

`estimated_pass^k = (1/M) × Σ_i [C(c_i,k) / C(n,k)]`, với `n >= k`.

Khi n=k=3, pass^3 là tỷ lệ scenario đạt cả ba trial. Không lấy trung bình success toàn bộ rồi nâng lũy thừa vì độ khó khác nhau giữa case. Không nhầm pass^k (tất cả k trial thành công) với pass@k (ít nhất một thành công). τ-bench đề xuất đo reliability qua nhiều trial và đối chiếu outcome thực tế thay vì chỉ tin lời agent. [τ-bench](https://arxiv.org/abs/2406.12045).

Baseline/candidate phải chạy trên cùng case để so sánh paired. Báo confidence interval bằng resampling theo scenario; với online traffic, dùng đơn vị user/session theo cách randomization thực tế. Không coi nhiều paraphrase hoặc nhiều turn trong một session là các quan sát độc lập. Pilot giúp ước lượng variance, sau đó quyết định sample size theo mức chênh lệch nhỏ nhất cần phát hiện.

**7. Stack nên dùng**

| Công cụ | Vai trò đề xuất | Điều kiện |
|---|---|---|
| Python/pytest + existing evidence inspectors | Contract, trace assertions, runner và release gates | Phần nên tận dụng ngay, tương thích A2A/MCP hiện tại |
| Langfuse | Dataset, experiment comparison, score theo trace/session, human annotation | Repo đã cấu hình Langfuse/OTel; kiểm tra version triển khai trước khi chọn API |
| Ragas | Faithfulness và recall/precision ở phần RAG | Cần input/output/context/reference đầy đủ và judge đã hiệu chuẩn tiếng Việt |
| ADK eval / AgentEvals | Tham khảo metric, adapter trajectory khi có lợi | Không mặc định ADK runner có thể gọi trực tiếp SandboxAgent qua A2A; cần adapter và kiểm tra version |
| MLflow | NDCG và chất lượng BST | Tiếp tục giữ gắn với version model/data; có thể liên kết vào báo cáo workflow |

Langfuse hỗ trợ score bằng code, judge và người, cùng dataset experiments; có thể nhận metadata experiment qua OpenTelemetry. Nên dùng để tập hợp kết quả từ runner hiện tại. [Langfuse evaluation](https://langfuse.com/docs/evaluation/core-concepts), [experiments qua OTel](https://langfuse.com/docs/evaluation/experiments/experiments-via-opentelemetry).

Chi tiết integration cần chú ý: [collector hiện tại](/Users/KHOAI/anhkhoa/RecSys-MLops/infra/helm/recsys-observability/templates/otel-collector.yaml:75) có nhánh semantic Langfuse giữ input/output đã redaction, còn nhánh operational chủ yếu giữ metadata. Langfuse allowlist chưa có các field `langfuse.experiment.*`. Nếu dùng experiment qua OTel, cần kiểm tra mapping/allowlist; chỉ thêm attribute ở emitter có thể vẫn bị collector loại bỏ. Cũng cần nối invocation với parent/child trace và release/dataset version. Source configuration không bảo đảm runtime đã phát đầy đủ các field nội dung.

Giữ redaction đang có. Exact-ID/args assertions nên chấm trên typed evidence trong môi trường tin cậy trước khi xuất score. Semantic grader nhận dữ liệu đã xử lý nhất quán; nếu redaction làm mất evidence cần thiết thì ghi unknown, không suy diễn điểm. Dữ liệu eval tổng hợp giúp kiểm thử prompt injection mà không cần lưu nội dung nhạy cảm thật.

**8. Rubric và kiểm định judge**

Judge chỉ xử lý tiêu chí ngữ nghĩa, ví dụ mỗi mục 0/1/2: relevance, coverage và giải thích phù hợp evidence. Ghi reason ngắn cùng claim/chunk hỗ trợ kết luận. Faithfulness/citation support vẫn có score riêng để nhìn thấy lỗi factual, không hòa tan vào điểm văn phong.

Chấm mù variant/model và xáo thứ tự A/B khi đánh giá pairwise. Khởi đầu khoảng 30–50 output được người đọc gán nhãn, gồm cả output đúng, sai, thiếu evidence và báo lỗi đúng; hiệu chuẩn rubric trước khi dùng làm blocking gate. Đo false-pass/false-fail và xem từng bất đồng. Gắn version model judge, prompt, rubric, dataset vào kết quả. Judge timeout/schema lỗi là UNKNOWN.

Test chính grader bằng các thay đổi có chủ đích: đảo thứ tự item, sửa một score, thay chunk ID, thêm claim không có nguồn, bỏ một child trace. Grader phải bắt lỗi; đồng thời chấp nhận JSON khác thứ tự key và câu văn paraphrase vẫn đúng. Đây là kiểm chứng oracle, không phải benchmark model.

**9. Gate và thứ tự thực hiện**

Đề xuất dưới đây là policy khởi đầu cho dự án; chưa phải ngưỡng đã hiệu chuẩn:

1. **Evidence gate:** mọi mandatory case phải có trace/child evidence đầy đủ; thiếu evidence → HOLD. Báo `evaluation_coverage = evaluated / attempted` và unknown rate để không làm đẹp success rate bằng cách loại case không chấm được.
2. **Hard contract gate:** 100% assertion quan trọng trong regression suite phải đạt; không sai user/item ID, score/order, forbidden calls hoặc citation ID bịa. Vi phạm quan sát được → FAIL.
3. **Quality gate:** báo task success theo intent, faithfulness, citation support và completeness riêng. Sau pilot, chốt floor và non-inferiority margin trước khi chạy holdout; chỉ PASS khi confidence interval hỗ trợ tiêu chí. Kết quả chưa đủ rõ → HOLD, không gọi là “không regression”.
4. **Reliability/performance gate:** báo pass^3, error/degradation và p95 theo intent. Có thể giữ mức cảnh báo latency tăng 20% đang có để làm baseline vận hành, nhưng cần đủ samples và điều kiện tải tương đương để ra kết luận.

Thứ tự ưu tiên triển khai:

- **P0 — evidence và contracts:** tách invariant evaluator khỏi fixture-only evaluator; thu evidence theo turn; chuẩn hóa parent-child trace; giữ 20-case smoke và hard gates hiện có.
- **P1 — offline quality:** tạo dataset tiếng Việt, runner paired/repeated và tool fixtures; thêm outcome/citation graders; log score vào Langfuse; thử judge trên tập đã được người đọc kiểm tra.
- **P2 — release và production:** thêm quality stage trước promotion, mở rộng organic scoring theo intent, lấy mẫu semantic eval bất đồng bộ; tiếp tục runtime/error monitoring toàn traffic.

Runner quality nên là job riêng. [Jenkins workflow hiện tại](/Users/KHOAI/anhkhoa/RecSys-MLops/jenkins/LLMWorkflowCD.Jenkinsfile) và router có quy tắc 20 case/không replay; không tăng số lần chạy bằng cách gửi lại request synthetic vào đường rollout này. Ngoài ra [workflow validation](/Users/KHOAI/anhkhoa/RecSys-MLops/jenkins/python/llm_agent_cd/workflow.py:79) đang giữ cố định agent template/tools/overrides trong các experiment mode hiện tại; muốn thử prompt hoặc topology cần experiment path hỗ trợ thay đổi đó.

Với A/B online, tách traffic synthetic/live-test/organic, phân bổ nhất quán theo user hoặc session, và theo dõi event outcome thực như người dùng chọn item nếu sản phẩm đã instrument được. Khi đo tác động agent, giữ BST và RAG index cố định; nếu cùng đổi chúng, kết quả là tác động toàn stack. Quan sát click không tự tạo ground truth cho độ đúng của giải thích.

Deliverable đầu tiên nên là bảng baseline/candidate có: dataset/release/index/model version, số scenario/trial, evidence coverage, task success theo intent, contract violations, grounding/citation/completeness, pass^3, p95 và token/cost per successful task. Chỉ ghi các giá trị đã đo; inference cost, judge cost và UNKNOWN phải được công bố riêng.
