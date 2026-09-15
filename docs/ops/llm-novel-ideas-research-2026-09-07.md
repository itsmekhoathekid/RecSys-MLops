**Novel ideas cho LLM scope — nghiên cứu ngày 07/09/2026**

Đề xuất ưu tiên hai thí nghiệm: (1) giới hạn tool theo trạng thái workflow kết hợp schema-constrained generation; (2) chọn evidence theo độ phủ item và ngân sách token, với prompt compression là một biến thể so sánh. Nếu ưu tiên nghiên cứu tối ưu prompt, chọn GEPA thay cho hướng thứ nhất sau khi có evaluation dataset đáng tin cậy.

Đây là đề xuất dựa trên workbook, working-tree source và tài liệu nghiên cứu. Chưa triển khai các ý tưởng, chạy benchmark mới, hoặc xác minh cấu hình production. Các ngưỡng bên dưới là mục tiêu thử nghiệm đề xuất, không phải kết quả đã đạt.

**Yêu cầu trong workbook**

Nguồn: [Coursework Tracking (Public).xlsx](</Users/KHOAI/anhkhoa/RecSys-MLops/docs/xlsx/Coursework Tracking (Public).xlsx>), tab thứ ba tên `Sheet3`.

- `A61`: Novel ideas không bắt buộc là sáng tạo thuật toán mới; có thể nghiên cứu thêm công cụ/kỹ thuật không được dạy ở EDAI.
- `B61:E62`: hai ý tưởng, mỗi ý tưởng 2 điểm; deliverable là “Document idea + proof it worked!”.
- `A50:E57`: observability và A/B config/model đã là yêu cầu riêng. Warm-up cũng là yêu cầu riêng ở `A26:E26`.

Diễn giải để chọn đề tài: nên có câu hỏi cụ thể, thay đổi kỹ thuật rõ, baseline và bằng chứng định lượng; thêm dashboard hoặc chạy A/B đơn thuần dễ trùng phần bắt buộc. Workbook không liệt kê toàn bộ nội dung EDAI đã dạy, nên mức phù hợp rubric dưới đây là đánh giá dựa trên những mục hiện diện trong sheet, không phải cam kết điểm của giảng viên.

**Hiện trạng liên quan của repo**

| Bằng chứng | Ý nghĩa khi chọn đề tài |
|---|---|
| [Catalog Qwen3.5-0.8B Q8_0](/Users/KHOAI/anhkhoa/RecSys-MLops/configs/llm-ab/catalog/qwen3.5-0.8b-q8_0.json) khai báo llama.cpp, context 16,384, 2 threads | Ưu tiên cải thiện inference/runtime và context; chưa cần huấn luyện model lớn |
| [Context prompt](/Users/KHOAI/anhkhoa/RecSys-MLops/infra/helm/recsys-kagent-agent/values.yaml:20) vẫn hướng dẫn budget 4,096 token và top_k tối đa 2 khi có IDs | Có nhiều profile/giới hạn khác nhau; phải đo actual budget của release đang test, không khẳng định production chỉ có 4K |
| [Notebook evidence lịch sử](/Users/KHOAI/anhkhoa/RecSys-MLops/docs/submission/rubric-final-coursework-(final-llm)/agent_notebooks.md:88) ghi nhận call thiếu `query`, sau đó retry | Có failure mode tool generation cụ thể để thiết kế regression case; chưa chứng minh model hiện tại còn cùng lỗi |
| [Coordinator prompt](/Users/KHOAI/anhkhoa/RecSys-MLops/infra/helm/recsys-coordinator-agent/values.yaml:8) quy định Recommendation → Context và không retry | Có policy rõ để đưa vào state machine ở runtime |
| [RAG retrieval](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/api-serving/rag-api/src/recsys_rag_api/retrieval.py:55) chọn theo cosine, nhóm item, giữ tối đa hai chunk/item | Có baseline rõ cho evidence selection/compression; không rerank danh sách BST |
| [Workflow evaluator](/Users/KHOAI/anhkhoa/RecSys-MLops/jenkins/python/llm_agent_cd/workflow_evidence.py:53) có trusted parent/child assertions | Tận dụng làm oracle kiểm tra tool/order/args, bổ sung outcome/grounding |
| [Workflow validation](/Users/KHOAI/anhkhoa/RecSys-MLops/jenkins/python/llm_agent_cd/workflow.py:79) giữ template/tools/overrides cố định trong A/B mode hiện tại | Các đề tài đổi prompt/tool policy cần experiment runner/release path riêng |

Một prerequisite của RAG: [build_user_rag_context](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/agentic/recsys-feature-rag-mcp/src/recsys_feature_rag_mcp/server.py:117) chuyển `candidate_item_ids` cho feature client, nhưng `rag_client.retrieve` chỉ nhận query/top_k_items/filters. [RetrievalRequest/Filters](/Users/KHOAI/anhkhoa/RecSys-MLops/apps/api-serving/rag-api/src/recsys_rag_api/contracts.py:26) cũng chưa có item-ID filter. Vì vậy hiện chưa có bảo đảm semantic evidence thuộc đúng tập item được Recommendation trả. Sửa/định nghĩa contract này cho cả baseline và candidate trước; không ghi công cải thiện từ sửa lỗi đó cho prompt compression.

**Shortlist**

| Ưu tiên | Ý tưởng | Câu hỏi thử nghiệm | Độ khó tương đối và điều kiện |
|---|---|---|---|
| 1 | Tool hợp lệ theo state + schema-constrained generation | Có giảm invalid calls, gọi dư và sai thứ tự khi dùng model nhỏ? | Trung bình–cao; cần hook runtime hoặc adapter có trusted per-turn state |
| 2 | Evidence selection/compression theo coverage và token budget | Có giảm input tokens/latency mà vẫn giải thích đủ item và giữ grounding? | Trung bình; cần aligned item evidence và semantic evaluation |
| 3 | GEPA tối ưu prompt bằng trace feedback | Có cải thiện held-out task success so với prompt viết tay dưới cùng ngân sách tìm kiếm? | Trung bình–cao; cần dataset và nhiều rollout offline, có chi phí reflection model |
| 4 | Corrective retrieval có giới hạn và abstention | Khi retrieval thiếu/sai evidence, có tăng chất lượng câu trả lời với số bước bị chặn trên? | Trung bình–cao; cần relevance labels và đánh giá cả coverage lẫn abstention |
| 5 | RouteLLM / routing theo độ khó | Có giữ quality gần model mạnh trong khi giảm chi phí inference? | Cao hơn với repo này; cần hai model và traffic đủ đa dạng |

Độ khó là ước lượng triển khai theo source hiện tại, không phải thời gian benchmark đã đo. Hai hướng đầu bổ sung nhau: một hướng xử lý hành động agent, một hướng xử lý dữ liệu LLM dùng để trả lời.

**Idea 1 — State-aware tool constraints for reliable small-model agents**

Câu hỏi: với cùng model, prompt nền và dữ liệu, cưỡng chế tập action hợp lệ có giảm tool-policy violations và cải thiện task completion không?

Thiết kế đề xuất:

1. Giữ Pydantic/MCP validation sau generation. Sinh schema cho bước model chọn action/args, hoặc dùng native tool schema được backend thực sự hỗ trợ.
2. Runtime quản lý state từ tool responses đã xác thực. Khi composite cần recommendation, cung cấp tool phù hợp; sau Recommendation thành công chỉ mở Context; sau Context kết thúc không mở thêm tool.
3. Khi thiếu user ID hoặc input không hợp lệ, cho phép hỏi bổ sung/báo lỗi. Không ép tool call bằng ID tự đoán. Với failure/partial outcome, có transition kết thúc đúng policy hiện tại.
4. Trước thực thi vẫn kiểm tra giá trị user ID, candidate IDs, top_k và release binding. Schema hợp lệ không chứng minh args đúng ngữ nghĩa.
5. Trạng thái gắn với invocation/turn, không rò giữa session. State chuyển theo actual function response, không theo lời model tự kể.

Llama.cpp có GBNF và chuyển một tập con JSON Schema sang grammar; server có schema-constrained responses và function calling. Đây là khả năng upstream, chưa xác nhận image digest trong repo và đường kagent/ADK đang truyền được mọi field. Cần compatibility spike kiểm tra raw request, tokenizer/chat template, required/nullable fields và tool-call parsing. `response_format` cho final JSON không tự động tương đương constrained native tool arguments. [llama.cpp grammar](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md), [server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), [function calling](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md).

Nếu native tool calling đã dùng grammar trong baseline, giữ nó làm baseline; đóng góp mới là policy theo state và kiểm soát dataflow, không gọi việc bật lại capability sẵn có là novelty. Có thể học thiết kế constrained decoding từ [XGrammar](https://arxiv.org/abs/2411.15100), nhưng không cần chuyển backend sang XGrammar chỉ để có tên công cụ mới.

**Experiment:** so sánh B0 = runtime hiện tại, B1 = thêm schema constraint nếu chưa có, B2 = schema + state-based tool policy. Thêm B3 = state policy không thêm schema nếu cần tách riêng tác động. Giữ model, token budget, tools thực, dữ liệu và test cases như nhau.

**Metrics:** invalid-argument rate; forbidden/duplicate-call rate; trajectory success; đúng user/item args; end-to-end success; pass^3; p95 và output tokens. Tách action bị chặn, action thực thi sai và task thành công: một controller chặn hết mọi action có violation thấp nhưng không hữu ích.

**Proof để nộp:** bảng paired comparison; trace trước/sau của case lỗi; state transitions; ảnh Langfuse/Grafana; raw evaluator report có release/dataset hashes; cả success case và boundary/failure case. Đạt khi giảm lỗi mà không tăng hành vi hỏi lại vô ích hoặc giảm task success; nếu baseline đã bão hòa, tập trung natural-language/hard cases độc lập với prompt tuning.

Điểm cần trình bày trung thực: đây là tăng độ tin cậy toàn system nhờ runtime constraints. Không diễn giải thành model tự học planning tốt hơn.

**Idea 2 — Coverage-aware evidence selection under a token budget**

Câu hỏi: trong cùng ngân sách context, phân bổ evidence theo item/claim có tăng grounded explanation coverage? Khi giữ quality tương đương, có giảm token và E2E latency?

Thiết kế đề xuất tại Context/MCP presentation boundary:

1. Lấy evidence thuộc đúng các item cần giải thích; giữ ranking BST như cũ.
2. Tách vùng phải giữ nguyên: user/item/chunk IDs, score, model/index version, giá, stock và các fact structured. Chỉ chọn/nén phần prose; không nén hoặc bỏ system instructions/tool schemas.
3. Ưu tiên tối thiểu một đoạn có ích cho mỗi item trước, sau đó thêm câu trả lời các thuộc tính người dùng hỏi. Loại nội dung lặp. Không dùng cosine score đơn thuần làm xác suất “đủ evidence”.
4. Tính budget theo tokenizer thực: context limit trừ system prompt, tool schemas, history, phần structured, output reserve và reasoning reserve nếu áp dụng. Không coi token budget trong prompt là giới hạn đã được enforce.
5. Giữ provenance bằng `chunk_id` và sentence/offset; nếu item không có evidence hoặc budget không đủ, thể hiện thiếu rõ ràng. Chấm incompleteness riêng với faithfulness.

LLMLingua-2 tiếp cận prompt compression bằng token classification từ dữ liệu distillation. Có thể dùng làm biến thể so sánh với bộ chọn câu extractive đơn giản; repo chính thức có model và ví dụ sử dụng. Tốc độ/quality trong paper không phải dự báo cho tiếng Việt, CPU và model của dự án. [Paper LLMLingua-2](https://arxiv.org/abs/2403.12968), [implementation](https://github.com/microsoft/LLMLingua).

Lost in the Middle cho thấy vị trí evidence có thể ảnh hưởng chất lượng xử lý context. Có thể thêm ablation đặt evidence quan trọng ở đầu so với thứ tự hiện tại; phải kiểm chứng trên model đang dùng. [TACL paper](https://aclanthology.org/2024.tacl-1.9/).

**Experiment:** sau khi áp dụng prerequisite item alignment giống nhau cho mọi arm, dùng B0 = payload hiện tại/cắt theo rule hiện tại; B1 = chọn câu theo coverage và budget; B2 = cùng pipeline + LLMLingua-2. Dùng candidate chunk pool giống nhau và thử vài mức budget vừa với cấu hình thực. Như vậy phân biệt lợi ích selection với lợi ích compressor.

**Metrics:** input tokens; preparation/compression time; TTFT và E2E p95; token overflow/truncation; factual/ID preservation; citation support; item explanation coverage; task success. Trường hợp compressor tiết kiệm token nhưng làm CPU latency tăng phải báo đúng.

**Mục tiêu pilot đề xuất:** giảm ít nhất 20% median input tokens so với B0, không có ID/score/price corruption quan sát được, và không giảm quá 2 percentage points task success/coverage theo margin chốt trước. Với tập pilot nhỏ, chưa đủ evidence thống kê thì báo chưa kết luận. Không lấy faithfulness cao hơn do bỏ hết lời giải thích làm chiến thắng.

**Proof để nộp:** quality–latency/token curve; bảng coverage theo số item cần giải thích; ví dụ prompt trước/sau giữ provenance; confidence interval theo scenario; CPU/RAM của compressor và end-to-end trace.

Nếu payload thực đã ngắn, compression có thể không đáng chi phí. Khi đó coverage-aware selection vẫn là giả thuyết cần đo, hoặc chuyển sang GEPA thay vì cố tạo benchmark với context dài không đại diện.

**Idea 3 — GEPA: tối ưu prompt từ execution feedback**

GEPA dùng phản hồi ngôn ngữ từ execution trajectories để đề xuất, thử và chọn prompt tốt hơn. Upstream có adapter framework và MCP adapter; kagent/A2A thực tế vẫn cần integration kiểm tra riêng. [GEPA paper](https://arxiv.org/abs/2507.19457), [GEPA repository](https://github.com/gepa-ai/gepa).

Ứng dụng vào repo: chỉ tối ưu phần hướng dẫn và examples của Coordinator/Context trước. Giữ immutable policy bên ngoài vùng optimizer được sửa: không đổi ranking, không bịa user ID/evidence, không thêm tool permissions, không vượt số lần gọi. Feedback cụ thể như “sau Recommendation đã gọi lại Recommendation” hữu ích hơn một điểm 0 chung chung.

So sánh prompt gốc, prompt chỉnh tay và GEPA với ngân sách search/rollout được công bố. Tách train/dev/holdout theo họ scenario; khóa holdout trước khi tối ưu. Đánh giá task success, pass^3, tokens và hard violations. Tổng chi phí phải gồm reflection model, rollout và judge.

Đây là lựa chọn có câu chuyện nghiên cứu rõ: “automated prompt optimization improves local small-model agent reliability”. Tuy nhiên 20 fixture chỉ dẫn sẵn tool chưa đủ để tối ưu và tuyên bố tổng quát hóa; cần dataset câu hỏi tự nhiên và evaluator đáng tin. Runtime không tự thay prompt production; optimizer xuất candidate artifact, evaluation/release flow quyết định sau.

**Idea 4 — Corrective retrieval có giới hạn**

CRAG đánh giá chất lượng retrieval và dùng corrective actions khi evidence yếu. [Paper CRAG](https://arxiv.org/abs/2401.15884). Biến thể phù hợp repo: kiểm tra item coverage/claim relevance; nếu thiếu thì thực hiện thêm một retrieval chiến lược khác trong phạm vi catalog, sau đó trả lời có nguồn hoặc abstain.

Có thể đặt corrective search bên trong một lần gọi MCP để không tăng số lần agent gọi tool, nhưng vẫn phải log/count toàn bộ backend requests và version hành vi mới. Không mô tả là triển khai đầy đủ CRAG nếu bỏ các thành phần như external search hoặc learned evaluator.

So sánh baseline, correction-only, correction+abstention trên clean/missing/noisy contexts. Chấm task success, unsupported claim rate, correct abstention, coverage, extra retrieval count và latency. Lỗi chính cần tránh: “không biết” cho mọi câu hỏi để có hallucination rate bằng 0.

**Idea 5 — Routing model theo độ khó/cost**

RouteLLM nghiên cứu lựa chọn giữa model mạnh và nhẹ bằng preference data để cân bằng cost/quality. [Paper](https://arxiv.org/abs/2406.18665), [implementation](https://github.com/lm-sys/RouteLLM).

Trong repo có thể thử model nhẹ cho tác vụ presentation đơn giản, model mạnh hơn cho composite/giải thích khó. So sánh all-small, all-large, rule router, learned router. Chấm task success, pass^3, cost/success và p95; cả router overhead phải được tính.

Khác với A/B trong sheet: A/B phân bổ traffic để so sánh version; routing chọn model theo request để tối ưu phục vụ. Repo hiện dùng một `llm` chung trong workflow release; per-role hoặc per-request model routing cần mở rộng release identity, bindings và evidence. Router pretrained ngoài domain không bảo đảm tốt với tiếng Việt và hai model local. Vì vậy đây là hướng mở rộng sau khi đủ capacity và labeled examples, chưa phải lựa chọn đầu tiên.

**Evaluation chung và bộ bằng chứng**

Dùng [nghiên cứu evaluation trước đó](/Users/KHOAI/anhkhoa/RecSys-MLops/docs/ops/agent-evaluation-research-2026-09-07.md) làm nền. Khởi đầu 40–60 scenario có oracle rõ, chia thành cases đơn giản, composite, thiếu ID, malformed/failed tools, missing/noisy evidence và multi-turn. Pilot 3 trial/case/arm là để đo consistency và ước lượng variance, không mặc định đủ power cho cải thiện nhỏ.

Pin model digest, quantization, generation parameters, prompt/policy version, tool schema version, ranker version, RAG index và feature snapshot. Chạy paired cùng scenario trên các arm với session mới, thứ tự được cân bằng. Tách warm/cold và tải khi đo latency. Bootstrap theo scenario hoặc cluster phù hợp; không coi các lượt trong cùng hội thoại là độc lập.

Sử dụng code cho ID/order/schema/trajectory, human-calibrated judge cho claim support và completeness. Báo UNKNOWN/evidence coverage; semantic score không được bù hard violation. Chạy negative controls để biết evaluator thật sự bắt được item đảo thứ tự, score bị sửa, citation bịa và trace thiếu.

Mỗi ý tưởng nên có một mục tài liệu trong phần submission LLM gồm: problem, reference technique, phần áp dụng/thay đổi của dự án, baseline, implementation diagram, reproducible command, dataset/split, experiment table, traces/screenshots và limitations. Chỉ đánh dấu “worked” khi có kết quả đã đo đáp ứng tiêu chí chốt trước. Nếu kết quả không cải thiện thì trình bày negative result và chọn hướng khác để đáp ứng yêu cầu proof.

Khuyến nghị hành động tiếp theo: compatibility spike cho Idea 1 và kiểm tra item-aligned evidence/payload size cho Idea 2. Hai kiểm tra này cho biết nên đầu tư vào runtime constraints + evidence budgeting hay chuyển sang GEPA, trước khi triển khai experiment đầy đủ.
