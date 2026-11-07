# ConstraintLens — Literature Survey

Oct 28, 2026 · @Danielraj

## Scope and Method

This survey reviews literature relevant to ConstraintLens, a runtime framework for detecting semantic constraint drift in asynchronous multi-agent AI systems. Searches covered IEEE Xplore, ACM Digital Library, arXiv, SpringerLink, ScienceDirect, ACL Anthology, Semantic Scholar and Google Scholar, plus NeurIPS, ICML, ICLR, AAAI, IJCAI, AAMAS, ICSE, FSE and RV proceedings, for 2022–2026 with earlier foundational work where needed.

Nine themes were searched separately: (1) LLM multi-agent frameworks, (2) multi-agent failure taxonomies, (3) instruction, goal and persona drift, (4) runtime verification and monitoring of agents, (5) guardrails and constraint compliance, (6) semantic consistency and NLI-based checking, (7) consistency and ordering in asynchronous distributed systems, (8) agent protocols and observability, and (9) concept and specification drift.

Verification notes: every entry was located through search and carries a DOI or URL. Preprints are labelled "arXiv preprint". Entries marked \[abstract only\] were assessed from their abstract or publisher landing page, and no claim beyond that is made.

## Part 1: Literature Survey Table

Fifty-two sources are surveyed in two tables. Table 1A covers the nine background themes \[1\]–\[34\]; Table 1B covers direct prior work and the literature behind specific ConstraintLens design choices \[35\]–\[52\]. Nine are IEEE publications (\[5\], \[15\], \[18\], \[28\], \[32\], \[33\], \[34\], \[38\], plus \[20\] at the IEEE/ACM ICSE conference). Rows are numbered to match the in-text citations in Part 2 and the reference list in Part 5. The Limitations column records what each paper does not cover relative to semantic constraint drift; it is an assessment, not a quotation.

### Table 1A: Background literature \[1\]–\[34\]

| S.No | Title | Authors | Year | Venue/Publisher | Problem Addressed | Methodology/Approach | Key Findings | Limitations |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | [AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation](https://arxiv.org/abs/2308.08155) | Q. Wu, G. Bansal, J. Zhang, Y. Wu, S. Zhang, E. Zhu, B. Li, L. Jiang, X. Zhang, C. Wang | 2023 | arXiv preprint 2308.08155 | Building LLM applications from multiple cooperating agents | Customizable "conversable" agents that combine LLMs, human input and tools; programmable conversation patterns | Complex workflows can be expressed as automated agent chats across coding, maths, QA and other domains | Preprint. No mechanism to check that task constraints survive agent-to-agent turns |
| 2 | [MetaGPT: Meta Programming for a Multi-Agent Collaborative Framework](https://proceedings.iclr.cc/paper_files/paper/2024/hash/6507b115562bb0a305f1958ccc87355a-Abstract-Conference.html) | S. Hong, M. Zhuge, J. Chen, X. Zheng, Y. Cheng, et al. | 2024 | ICLR 2024 | Logic inconsistencies from cascading hallucinations when LLMs are chained naively | Encodes Standard Operating Procedures into prompts; role-based assembly line; structured intermediate documents; publish–subscribe messaging | Produces more coherent solutions than earlier chat-based multi-agent systems on collaborative software-engineering benchmarks | Consistency relies on a fixed SOP structure; original requirements are not re-checked at each hand-off |
| 3 | [CAMEL: Communicative Agents for "Mind" Exploration of Large Language Model Society](https://proceedings.neurips.cc/paper/2023/hash/a3621ee907def47c1b952ade25c67698-Abstract-Conference.html) | G. Li, H. Hammoud, H. Itani, D. Khizbullin, B. Ghanem | 2023 | NeurIPS 2023 | Autonomous agent cooperation without constant human steering | Role-playing with inception prompting; generation of task-oriented conversational datasets | Role-playing keeps agents oriented toward the task and yields instruction-following multi-agent datasets | Mainly two-agent settings; alignment with human intent is maintained by prompting, not monitored |
| 4 | [Large Language Model Based Multi-Agents: A Survey of Progress and Challenges](https://doi.org/10.24963/ijcai.2024/890) | T. Guo, X. Chen, Y. Wang, R. Chang, S. Pei, N. V. Chawla, O. Wiest, X. Zhang | 2024 | IJCAI 2024 (Survey Track) | Organizing fast-growing LLM multi-agent research | Survey of environments, agent profiling, communication methods, capability growth and benchmarks | Structured overview of LLM-MA systems and open challenges | Descriptive survey; runtime verification of constraints is not a focus |
| 5 | [Self-Adaptive Large Language Model (LLM)-Based Multiagent Systems](https://arxiv.org/abs/2307.06187) | N. Nascimento, P. Alencar, D. Cowan | 2023 | IEEE ACSOS-C 2023, pp. 104–109 | Combining self-adaptation with expressive agent communication | Embeds GPT-4 in a per-agent MAPE-K (monitor–analyse–plan–execute) loop; small prototype MAS | Shows LLM-driven self-adaptation of agents is feasible | Illustrative prototype; monitoring serves adaptation, not constraint fidelity |
| 6 | [Why Do Multi-Agent LLM Systems Fail?](https://doi.org/10.48550/arXiv.2503.13657) | M. Cemri, M. Z. Pan, S. Yang, L. A. Agrawal, B. Chopra, R. Tiwari, K. Keutzer, A. Parameswaran, D. Klein, K. Ramchandran, M. Zaharia, J. E. Gonzalez, I. Stoica | 2025 | NeurIPS 2025 (arXiv 2503.13657) | Why multi-agent gains over single agents are often small | Grounded analysis of traces from 7 frameworks with expert annotators (κ = 0.88); validated LLM-as-judge annotator | MAST: 14 failure modes in 3 categories (system design, inter-agent misalignment, task verification), including disobeying role specification, loss of conversation history and task derailment; MAST-Data of 1,600+ traces | Post-hoc trace labelling; a taxonomy, not a live detector |
| 7 | [Which Agent Causes Task Failures and When? On Automated Failure Attribution of LLM Multi-Agent Systems](https://arxiv.org/abs/2505.00212) | S. Zhang, M. Yin, J. Zhang, J. Liu, Z. Han, J. Zhang, B. Li, C. Wang, et al. | 2025 | ICML 2025, PMLR 267 | Locating the agent and step responsible for a failure | Who&When dataset of failure logs from 127 multi-agent systems; three LLM-based attribution strategies (all-at-once, step-by-step, binary search) | Automated attribution is hard; even strong reasoning models struggle | Operates after a failed run; attributes task failure, not violation of a specific constraint |
| 8 | [On the Resilience of LLM-Based Multi-Agent Collaboration with Faulty Agents](https://proceedings.mlr.press/v267/huang25ay.html) | J. Huang, J. Zhou, T. Jin, X. Zhou, Z. Chen, W. Wang, Y. Yuan, M. R. Lyu, M. Sap | 2025 | ICML 2025, PMLR 267 | How errors from faulty agents propagate across team structures | AutoTransform and AutoInject inject errors; compares linear, flat and hierarchical structures; Challenger and Inspector defences | Hierarchical structure is most resilient (5.5% drop vs 10.5% and 23.7%); defences recover up to 96.4% of errors | Synthetic injected errors; no explicit model of task constraints |
| 9 | [Measuring and Controlling Instruction (In)Stability in Language Model Dialogs](https://arxiv.org/abs/2402.10962) | K. Li, T. Liu, N. Bashkansky, D. Bau, F. Viégas, H. Pfister, M. Wattenberg | 2024 | COLM 2024 | Whether system-prompt instructions stay in force over a dialog | Self-chat benchmark between two instructed chatbots; attention analysis; split-softmax mitigation | Significant instruction drift within eight rounds (LLaMA2-chat-70B, GPT-3.5); attention decay implicated | Two-party chat; deterministic checks of simple instructions; no multi-agent hand-offs |
| 10 | [Evaluating Goal Drift in Language Model Agents](https://doi.org/10.1609/aies.v8i1.36541) | R. Arike, E. Donoway, H. Bartsch, M. Hobbhahn | 2025 | AAAI/ACM AIES 2025, vol. 8(1), pp. 192–203 | Measuring an agent's tendency to drift from its original goal | Goal set in system prompt, then competing objectives introduced in a simulated trading environment over 100k+ tokens | All models drift to some degree; best scaffolded agent stays near-perfect beyond 100k tokens; drift linked to pattern-matching deeper in context | Single agent, single environment; task-specific drift score |
| 11 | [LLMs Get Lost in Multi-Turn Conversation](https://doi.org/10.48550/arXiv.2505.06120) | P. Laban, H. Hayashi, Y. Zhou, J. Neville | 2025 | arXiv 2505.06120; listed as ICLR 2026 | Performance when requirements arrive across turns | "Sharded" simulation revealing an instruction piece by piece; 15 LLMs, 200k+ simulated conversations, six tasks | Average 39% drop vs single-turn, driven mostly by unreliability; early wrong assumptions are not recovered | Human–LLM dialog, not agent–agent; measures degradation but offers no runtime detector |
| 12 | [Lost in the Middle: How Language Models Use Long Contexts](https://doi.org/10.1162/tacl_a_00638) | N. F. Liu, K. Lin, J. Hewitt, A. Paranjape, M. Bevilacqua, F. Petroni, P. Liang | 2024 | TACL vol. 12, pp. 157–173 | How well language models use information across long inputs | Controlled multi-document QA and key-value retrieval with the relevant item moved through the context | U-shaped accuracy: best at the start or end of context, worst in the middle | Retrieval tasks; effects on constraints buried mid-conversation are an inference, not tested |
| 13 | [Inherited Goal Drift: Contextual Pressure Can Undermine Agentic Goals](https://arxiv.org/abs/2603.03258) | A. Menon, M. Saebo, T. Crosse, S. Gibson, E. Jang, D. Cruz | 2026 | arXiv 2603.03258 (ICLR 2026 Lifelong Agents Workshop) | Whether recent models still drift from assigned goals | Re-uses the trading environment of \[10\]; conditions models on prefilled trajectories from weaker agents | Robustness is brittle: models often inherit drift from weaker agents' trajectories; only GPT-5.1 stayed consistently resilient among those tested | Workshop paper; one environment; no detection method |
| 14 | [A Brief Account of Runtime Verification](https://doi.org/10.1016/j.jlap.2008.08.004) | M. Leucker, C. Schallhart | 2009 | J. Logic and Algebraic Programming 78(5), pp. 293–303 (Elsevier) | Defining runtime verification as a field | Conceptual overview of monitors and their relation to other verification techniques | Foundational reference for checking executions against specifications \[abstract only\] | Classical formal properties over discrete events; natural-language meaning out of scope |
| 15 | [A Runtime Verification Framework for Dynamically Adaptive Multi-Agent Systems](https://doi.org/10.1109/BIGCOMP.2016.7425981) | Y. J. Lim, G. Hong, D. Shin, E. Jee, D.-H. Bae | 2016 | IEEE BigComp 2016, pp. 509–512 | Ensuring safe adaptations in collaborative adaptive MAS | DAMS-RV: verification triggered by an adaptive feedback loop after each adaptation; nurse-agent example | Case study suggests runtime verification is feasible for such systems \[abstract only\] | Pre-LLM symbolic agents; single case study |
| 16 | [RV4JaCa – Runtime Verification for Multi-Agent Systems](https://doi.org/10.4204/EPTCS.362.5) | D. C. Engelmann, A. Ferrando, A. R. Panisson, D. Ancona, R. H. Bordini, V. Mascardi | 2022 | AREA 2022, EPTCS 362, pp. 23–36 | Adding a safety layer over agent message exchange, including dialog flow | Monitors JaCaMo agent messages against interaction protocols; hospital bed-allocation case study | Protocol compliance checked at runtime without modifying agents | Protocol and ordering properties; BDI agents, not LLM agents |
| 17 | [Mind the Gap! Runtime Verification of Partially Observable MASs with Probabilistic Trace Expressions](https://doi.org/10.1007/978-3-031-20614-6_2) | D. Ancona, A. Ferrando, V. Mascardi | 2022 | EUMAS 2022, LNCS 13442, pp. 22–40 (Springer) | Monitoring protocols when some interactions are not observed | Probabilistic trace expressions with state estimation; centralized monitoring algorithm | Estimates the probability of protocol states despite observation gaps \[abstract only\] | Checks interaction structure, not message meaning |
| 18 | [SOTER: A Runtime Assurance Framework for Programming Safe Robotics Systems](https://arxiv.org/abs/1808.07921) | A. Desai, S. Ghosh, S. A. Seshia, N. Shankar, A. Tiwari | 2019 | IEEE/IFIP DSN 2019 | Safe use of uncertified components in autonomous robots | Simplex-style runtime-assurance modules switching between advanced and safe controllers; publish–subscribe nodes | Formal guarantee for well-formed modules; safety maintained on real drones with faulty components | Numeric/physical safety specifications, not language-level constraints |
| 19 | [Runtime Monitoring Neuron Activation Patterns](https://past.date-conference.com/proceedings-archive/2019/html/0290.html) | C.-H. Cheng, G. Nührenberg, H. Yasuoka | 2019 | DATE 2019 | Detecting neural-network decisions unsupported by training data | Stores abstracted activation patterns; flags inputs whose pattern is far (Hamming distance) from stored ones | Reports a significant share of misclassifications with a small false-positive rate | White-box classifiers; no compositional or multi-agent setting |
| 20 | [AgentSpec: Customizable Runtime Enforcement for Safe and Reliable LLM Agents](https://doi.org/10.48550/arXiv.2503.18666) | H. Wang, C. M. Poskitt, J. Sun | 2026 | IEEE/ACM ICSE 2026 | Enforcing customizable safety constraints on LLM agents during execution | DSL rules of trigger, predicate and enforcement; hooks into LangChain; LLM-generated rules evaluated | Prevents over 90% of unsafe code executions, eliminates hazardous embodied actions, full AV compliance; millisecond overhead | Rules act on actions and tool calls with mostly symbolic predicates; single-agent focus |
| 21 | [ProbGuard: Probabilistic Runtime Monitoring for LLM Agent Safety](https://doi.org/10.48550/arXiv.2508.00500) | H. Wang, C. M. Poskitt, J. Wei, J. Sun | 2025 | arXiv preprint 2508.00500 | Reactive rules catch violations only when imminent | Proactive monitoring via probabilistic risk prediction | Anticipates safety violations before they occur \[abstract only\] | Safety states, not semantic constraint content; preprint |
| 22 | [Enforcing Temporal Constraints for LLM Agents](https://doi.org/10.48550/arXiv.2512.23738) | A. Kamath, S. Zhang, C. Xu, S. Ugare, G. Singh, S. Misailovic | 2025 | arXiv preprint 2512.23738 | Guardrails give no formal guarantee on temporal policies | Agent-C: temporal specs translated to first-order logic and checked by SMT during generation, with corrective feedback | Run-time guarantees that agents satisfy formal temporal safety properties | Needs structured tool calls; single agent; preprint |
| 23 | [NeMo Guardrails: A Toolkit for Controllable and Safe LLM Applications with Programmable Rails](https://doi.org/10.18653/v1/2023.emnlp-demo.40) | T. Rebedea, R. Dinu, M. N. Sreedhar, C. Parisien, J. Cohen | 2023 | EMNLP 2023 System Demonstrations (ACL), pp. 431–445 | Adding user-defined, model-independent rails to LLM apps | Proxy runtime acting as a dialog manager; rails written in Colang | Interpretable programmable rails work across several LLM providers | One user–bot conversation; flow-based rules; no inter-agent tracking |
| 24 | [FollowBench: A Multi-level Fine-grained Constraints Following Benchmark for Large Language Models](https://doi.org/10.18653/v1/2024.acl-long.257) | Y. Jiang, Y. Wang, X. Zeng, W. Zhong, L. Li, F. Mi, L. Shang, X. Jiang, Q. Liu, W. Wang | 2024 | ACL 2024, pp. 4667–4688 | Evaluating whether outputs satisfy each constraint in an instruction | Five constraint types; constraints added one level at a time; LLM judge using constraint-evolution paths | All tested models degrade as the number of constraints grows | Offline, single-model, single-turn benchmark |
| 25 | [SummaC: Re-Visiting NLI-based Models for Inconsistency Detection in Summarization](https://doi.org/10.1162/tacl_a_00453) | P. Laban, T. Schnabel, P. N. Bennett, M. A. Hearst | 2022 | TACL vol. 10, pp. 163–177 | NLI models had underperformed at inconsistency detection | Sentence-level NLI with score aggregation (SummaC-ZS, SummaC-Conv); new six-dataset benchmark | 74.4% balanced accuracy, five points above prior work | Document–summary pairs, not constraint–message pairs |
| 26 | [SelfCheckGPT: Zero-Resource Black-Box Hallucination Detection for Generative Large Language Models](https://doi.org/10.18653/v1/2023.emnlp-main.557) | P. Manakul, A. Liusie, M. J. F. Gales | 2023 | EMNLP 2023, pp. 9004–9017 | Detecting hallucination without model internals or external databases | Compares a response with sampled responses using BERTScore, QA, n-gram, NLI and prompting | Outperforms grey-box baselines; NLI variant offers a strong accuracy–cost balance | Self-consistency, not compliance with externally given constraints; needs multiple samples |
| 27 | [Time, Clocks, and the Ordering of Events in a Distributed System](https://doi.org/10.1145/359545.359563) | L. Lamport | 1978 | Communications of the ACM 21(7), pp. 558–565 | Ordering events without a shared physical clock | "Happened-before" partial order; logical clocks | Logical clocks yield a consistent total order of events | Scalar clocks do not fully characterize causality |
| 28 | [Logical Time: Capturing Causality in Distributed Systems](https://decomposition.al/CSE232-2021-09/readings/extra/raynal-singhal-logical-time-survey.pdf) | M. Raynal, M. Singhal | 1996 | IEEE Computer 29(2), pp. 49–56 | Tracking causality in asynchronous distributed computations | Framework for logical clocks: scalar, vector and matrix time | Richer clocks capture more causal information at higher cost | Tutorial; content of messages is out of scope |
| 29 | [AgentsNet: Coordination and Collaborative Reasoning in Multi-Agent LLMs](https://doi.org/10.48550/arXiv.2507.08616) | F. Grötschla, L. Müller, J. Tönshoff, M. Galkin, B. Perozzi | 2025 | arXiv preprint 2507.08616 | Whether LLM agent networks can coordinate over a topology | Benchmark built on classic distributed-systems problems; synchronous message passing; up to 100 agents | Frontier models do well on small networks but fall off as networks scale | Synchronous rounds; paper notes transfer to asynchronous settings is unclear |
| 30 | [A Survey of Agent Interoperability Protocols: MCP, ACP, A2A, and ANP](https://doi.org/10.48550/arXiv.2505.02279) | A. Ehtesham, A. Singh, G. K. Gupta, S. Kumar | 2025 | arXiv preprint 2505.02279 | Fragmented, ad-hoc agent integration | Comparative survey of four protocols across interaction modes, discovery, communication patterns and security | ACP supports both synchronous and asynchronous interaction; phased adoption roadmap proposed | Transport, discovery and security only; semantic content of messages not addressed |
| 31 | [AgentOps: Enabling Observability of LLM Agents](https://doi.org/10.48550/arXiv.2411.05285) | L. Dong, Q. Lu, L. Zhu | 2024 | arXiv preprint 2411.05285 | Lack of agent-specific observability | Systematic mapping of AgentOps tools; taxonomy of artifacts and spans to trace, including guardrail spans | Existing tools focus on LLM metrics and prompts, with limited tracing of goals, plans and tools | Specifies what to record, not how to judge semantic correctness |
| 32 | [Learning under Concept Drift: A Review](https://doi.org/10.1109/TKDE.2018.2876857) | J. Lu, A. Liu, F. Dong, F. Gu, J. Gama, G. Zhang | 2019 | IEEE Trans. Knowledge and Data Engineering 31(12), pp. 2346–2363 | Organizing concept-drift research | Review of 130+ publications; catalogue of datasets | Framework of drift detection, understanding (when, how, where) and adaptation | Statistical drift in data streams, not drift in meaning of instructions |
| 33 | [Learning in Nonstationary Environments: A Survey](https://doi.org/10.1109/MCI.2015.2471196) | G. Ditzler, M. Roveri, C. Alippi, R. Polikar | 2015 | IEEE Computational Intelligence Magazine 10(4), pp. 12–25 | Learning when the data-generating process changes | Survey contrasting active and passive adaptation | Non-adaptive models degrade or fail under nonstationarity \[abstract only\] | Distributional drift only |
| 34 | [An Overview on Concept Drift Learning](https://doi.org/10.1109/ACCESS.2018.2886026) | A. S. Iwashita, J. P. Papa | 2019 | IEEE Access vol. 7, pp. 1532–1547 | Surveying concept-drift handling | Review of drift types, learners, detection mechanisms and datasets | Catalogue of drift types and public datasets \[abstract only\] | Classification-centric; no language or agent setting |

### Table 1B: Direct prior work and design-specific literature \[35\]–\[52\]

These sources map onto the ConstraintLens design: the closest prior work it positions against \[35\]–\[39\], delegation and authorization schemes that check structure \[40\]–\[45\], the injection and judge-manipulation threats it defends against \[46\]–\[49\], and the evidence behind the tiered checker \[50\]–\[52\]. Limitations are stated relative to semantic drift across asynchronous hand-offs.

| S.No | Title | Authors | Year | Venue/Publisher | Problem Addressed | Methodology/Approach | Key Findings | Limitations |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 35 | [Safe Multi-Agent Behavior Must Be Maintained, Not Merely Asserted: Constraint Drift in LLM-Based Multi-Agent Systems](https://arxiv.org/abs/2605.10481) | T. Li, Y. Ma, H. Wen, Z. Huang, Q. Zhou, Z. Fu, G. Cheng | 2026 | arXiv preprint 2605.10481 (position paper; University of Liverpool and collaborators) | Safety constraints losing operational force across multi-agent trajectories | Defines constraint drift; compares mechanism families for agent safety; proposes Constraint State Governance as a research paradigm | Frames drift as a systems-level preservation failure across memory, delegation, communication and tool use; argues constraints must be kept as explicit state | Position paper: no implementation, detector or benchmark |
| 36 | [Models Recall What They Violate: Constraint Adherence in Multi-Turn LLM Ideation](https://arxiv.org/abs/2604.28031) (DRIFTBENCH) | G. Kruthof | 2026 | arXiv preprint 2604.28031; listed as a NeurIPS 2026 poster | Whether models keep hard constraints under iterative multi-turn pressure | 38 research briefs with hard constraints; 2,146 scored runs across 7 models and 4 interaction conditions; restatement probe; human validation | Models restate constraints they violate ("knows-but-violates" rate 8%–99%); 74% of drift cases violate by turn 2; checkpointing helps but does not close the gap; the LLM judge under-detects violations vs blind human raters | Single model, single conversation, scientific ideation; no agents, tools or asynchronous hand-offs |
| 37 | [ASTRA: Agentic Steerability and Risk Assessment Framework](https://arxiv.org/abs/2511.18114) | I. Hazan, Y. Mathov, G. Shtar, R. Bitton, I. Mantin | 2025 | arXiv preprint 2511.18114 (Intuit AI Security Research) | Whether LLMs enforce custom guardrails written in an agent's system prompt | Simulates ten autonomous tool-using agents and challenges them with policy-violating requests | Measures how well models support agents that enforce application-specific guardrails \[abstract only\] | Guardrail stated once per agent; no tracking across agents; evaluation framework, not a runtime monitor |
| 38 | [Operational Hallucination and Safety Drift in AI Agents](https://arxiv.org/abs/2607.18366) | S. Yu, F. Carroll, B. L. Bentley | 2026 | IEEE ICAD 2026 (accepted; arXiv 2607.18366) | Reliability risks when LLMs plan tool use over many turns | Diagnostic case study across several state-of-the-art LLMs | "Safety drift": declared safety intent erodes into constraint-violating actions; "operational hallucination": repetitive tool-call livelocks | Single-agent case study; characterises failures but provides no detector |
| 39 | [SafetyDrift: Predicting When AI Agents Cross the Line Before They Actually Do](https://arxiv.org/abs/2603.27148) | A. Dhodapkar, F. Pishori | 2026 | arXiv preprint 2603.27148 | Individually safe steps that compound into a violation | Models safety trajectories as absorbing Markov chains; closed-form probability of reaching a violation | All agents studied eventually reach violations when unsupervised; per-step monitors cannot see trajectory-level violations | Single agent; abstract safety states rather than the content of specific constraints |
| 40 | [Human Delegation Provenance Protocol (HDP): Cryptographic Chain-of-Custody for Agentic AI Systems](https://datatracker.ietf.org/doc/html/draft-helixar-hdp-agentic-delegation-00) | A. Dalugoda (Helixar) | 2026 | IETF Internet-Draft draft-helixar-hdp-agentic-delegation-00 (later revisions to -03) | Terminal actions becoming disconnected from the human authorization behind them | Ed25519-signed token binding a human authorization to a session; append-only chain with one signed entry per delegation hop; offline verification | Argues RFC 8693, JWT, UCAN and the Intent Provenance Protocol do not meet multi-hop human-provenance needs | Internet-Draft, not peer-reviewed; states it is not an authorization protocol; proves who delegated, not whether meaning survived |
| 41 | [Attenuating Authorization Tokens for Agentic Delegation Chains](https://www.ietf.org/archive/id/draft-niyikiza-oauth-attenuating-agent-tokens-00.txt) | N. A. Niyikiza (Tenuo) | 2026 | IETF Internet-Draft draft-niyikiza-oauth-attenuating-agent-tokens-00 (OAuth WG submission) | Delegated authority that can grow along an agent chain | JWT tokens listing permitted tools and argument constraints; each holder may only narrow authority; offline chain verification | Typed constraint vocabulary for tool-level argument restrictions | Internet-Draft; checks scope and tool arguments, not natural-language task text |
| 42 | [A Five-Plane Reference Architecture for Runtime Governance of Production AI Agents](https://arxiv.org/abs/2606.12320) | K. Tallam (Kamiwaza) | 2026 | arXiv preprint 2606.12320 | Policy enforcement split across separate infrastructure layers | Reasoning plane adjudicates intent; network, identity, endpoint and data planes realise one decision | Argues prior architectures (BeyondCorp, service mesh, stateless authorization) are special cases \[abstract only\] | Governs delegated actions, not model behaviour; authors leave benchmark evaluation to future work |
| 43 | [Vouch Protocol](https://github.com/vouch-protocol/vouch) | R. G. (Vouch Protocol project) | 2025–2026 | Open specification and W3C Credentials Community Group report draft (grey literature) | No cryptographic proof of which agent made an API call | Ed25519 signatures, decentralised identifiers and JWT-style tokens; agent signs each action; audit trail | Framework-agnostic agent identity and action signing for MCP, LangChain and CrewAI | Not peer-reviewed; identity and accountability only, no check of constraint meaning |
| 44 | [Intent-Governed Tool Authorization for AI Agents](https://arxiv.org/abs/2606.22916) | G. Zhu, C. Wang | 2026 | arXiv preprint 2606.22916 | Agent credentials broader than the user's current request | Converts a trusted request into a short-lived intent certificate; narrows the tool manifest; checks payload effects before execution | Cannot grant authority outside static policy; reduces an archived exposure indicator from 1.0 to 0 in a deterministic comparison | Certificate derived once from the initial request; confinement depends on certificate fidelity; no multi-hop drift |
| 45 | [Position: AI Agents Need Authenticated Delegation](https://proceedings.mlr.press/v267/south25a.html) | T. South, S. Marro, T. Hardjono, R. Mahari, C. D. Whitney, A. Chan, A. Pentland | 2025 | ICML 2025, PMLR 267, pp. 82211–82231 | Authorization and accountability when humans delegate to agents | Extends OAuth 2.0 and OpenID Connect with agent credentials; translates natural-language permissions into access-control rules | Clear chains of accountability compatible with existing web infrastructure | Position paper; scope fixed at delegation time, no runtime semantic check |
| 46 | [Not What You've Signed Up For: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection](https://doi.org/10.48550/arXiv.2302.12173) | K. Greshake, S. Abdelnabi, S. Mishra, C. Endres, T. Holz, M. Fritz | 2023 | ACM AISec 2023, pp. 79–90 | Attacks through data the model retrieves rather than the user prompt | Demonstrates indirect prompt injection against real LLM-integrated applications | Retrieved content can override instructions; threats include data theft, worming and unauthorized API calls | Single application; no account of injected instructions spreading across agent hand-offs |
| 47 | [AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents](https://doi.org/10.52202/079017-2636) | E. Debenedetti, J. Zhang, M. Balunović, L. Beurer-Kellner, M. Fischer, F. Tramèr | 2024 | NeurIPS 2024 Datasets and Benchmarks Track | Measuring agent robustness when tools return untrusted data | Extensible environment: 97 tasks, 629 security test cases, adaptive attacks and defenses | Models fail many tasks even without attack; attacks break some security properties but not all | Single agent; injection only, no drift labels or controls |
| 48 | [Defeating Prompt Injections by Design](https://doi.org/10.48550/arXiv.2503.18813) (CaMeL) | E. Debenedetti, I. Shumailov, T. Fan, J. Hayes, N. Carlini, D. Fabian, C. Kern, C. Shi, A. Terzis, F. Tramèr | 2025 | arXiv preprint 2503.18813 | Untrusted data hijacking an agent's control flow | Extracts control and data flow from the trusted query; capability metadata on values; policy checks before tool calls | Solves 77% of AgentDojo tasks with provable security vs 84% undefended | Single agent plan; enforces data-flow policy, not the meaning of reworded constraints |
| 49 | [Optimization-based Prompt Injection Attack to LLM-as-a-Judge](https://doi.org/10.1145/3658644.3690291) (JudgeDeceiver) | J. Shi, Z. Yuan, Y. Liu, Y. Huang, P. Zhou, L. Sun, N. Z. Gong | 2024 | ACM CCS 2024 | Manipulating an LLM judge's decision | Gradient-based optimization of an injected sequence placed in a candidate response | Far more effective than hand-crafted injections; known-answer and perplexity defenses are insufficient | Attack paper; motivates hardening any LLM judge used as a verifier |
| 50 | [Stress Test Evaluation for Natural Language Inference](https://aclanthology.org/C18-1198) | A. Naik, A. Ravichander, N. Sadeh, C. Rose, G. Neubig | 2018 | COLING 2018, pp. 2340–2353 | Whether NLI models reason or pattern-match | Error analysis plus automatically built stress tests (negation, antonymy, numerical reasoning, etc.) | Negation words push models toward "contradiction"; models fail numerical and quantifier reasoning | Older sentence-encoder models; current models need re-testing |
| 51 | [EQUATE: A Benchmark Evaluation Framework for Quantitative Reasoning in Natural Language Inference](https://doi.org/10.18653/v1/K19-1033) | A. Ravichander, A. Naik, C. Rose, E. Hovy | 2019 | CoNLL 2019, pp. 349–361 | Quantitative reasoning in textual entailment | Five test sets; nine published NLI models; symbolic baseline Q-REAS | NLI models on average do not beat a majority-class baseline on quantities; symbolic handling helps on synthetic sets | Pre-LLM models; symbolic baseline weak on verbal reasoning |
| 52 | [FrugalGPT: How to Use Large Language Models While Reducing Cost and Improving Performance](https://mlanthology.org/tmlr/2024/chen2024tmlr-frugalgpt/) | L. Chen, M. Zaharia, J. Zou | 2024 | Transactions on Machine Learning Research (TMLR) | High and uneven cost of LLM APIs | LLM cascade that learns which models to call for each query | Matches the best single LLM with up to 98% lower cost | Optimises task accuracy and cost, not safety verdicts with fail-closed behaviour |

### Positioning notes on prior work named in the overview

- **"Cisco ASTRA".** No Cisco paper named ASTRA was found. The closest match is ASTRA \[37\], from Intuit AI Security Research, which evaluates whether LLMs enforce system-prompt guardrails. A separate ASTRA (arXiv 2511.02356) is a jailbreak framework. Re-check the source before citing it as Cisco's.
- **Liverpool "Constraint Drift" paper.** Confirmed as \[35\]. It names and formalises constraint drift and calls for constraints to be kept as explicit state, but provides no implementation, which matches the overview.
- **DRIFTBENCH.** Confirmed as \[36\]: single model, multi-turn, no agents or asynchrony. Note that several unrelated projects use the name DriftBench or Drift-Bench, so keep the name driftchain-bench distinct.
- **IETF draft and HDP.** The IETF draft is HDP itself \[40\]. Related drafts include attenuating agent tokens \[41\] and a WIMSE agent delegation-chain draft (draft-asor-wimse-agent-delegation-chain-01). All are structural: they prove who delegated what scope, not whether meaning survived.
- **Five-Plane architecture.** Confirmed as \[42\], an arXiv preprint.
- **Vouch Protocol.** Confirmed as an open specification with a W3C Community Group report draft \[43\]. It is grey literature, so cite it only for positioning, not as evidence.

## Part 2: Thematic Literature Survey

### 2.1 LLM-Based Multi-Agent Frameworks and Coordination

Large language model (LLM) based multi-agent systems decompose a task among specialised agents that coordinate through natural-language messages. AutoGen \[1\] generalises this pattern into "conversable" agents whose interaction topology is programmed by the developer, while CAMEL \[3\] uses role-playing with inception prompting so that two agents can cooperate with minimal human intervention. MetaGPT \[2\] adds organisational structure by encoding Standard Operating Procedures into prompts, assigning roles along an assembly line and exchanging structured intermediate documents through a publish–subscribe mechanism.

The survey by Guo et al. \[4\] classifies such systems by environment, agent profiling, communication paradigm and capability acquisition, and confirms that inter-agent communication is the central design dimension. Nascimento et al. \[5\] place an LLM inside each agent's MAPE-K loop, showing that monitoring and adaptation can be delegated to the model itself.

These frameworks treat constraint preservation as a by-product of good prompting or workflow design. MetaGPT motivates its SOPs explicitly by the "cascading hallucinations" that arise when LLMs are chained naively \[2\], yet none of \[1\]–\[5\] provides an independent mechanism that checks, at each hand-off, whether the constraints stated at the start of the task are still respected. ConstraintLens is designed as such a framework-agnostic layer that observes the messages these systems already exchange.

### 2.2 Failure Analysis and Failure Taxonomies of Multi-Agent LLM Systems

Empirical work now shows that multi-agent LLM systems fail often and in structured ways. Cemri et al. \[6\] derive MAST from expert-annotated traces of seven frameworks and identify fourteen failure modes in three categories: system design issues, inter-agent misalignment and task verification. Several modes correspond directly to constraint drift, including disobeying the task or role specification, loss of conversation history, task derailment and ignoring another agent's input.

Zhang et al. \[7\] formalise automated failure attribution as identifying the responsible agent and the decisive error step, release the Who&When dataset, and report that even strong reasoning models find the task difficult. Huang et al. \[8\] inject faults into agents and messages and show that errors propagate differently across linear, flat and hierarchical structures, and that an "Inspector" agent that reviews messages can recover most injected errors.

Together, \[6\]–\[8\] establish that errors originate at specific agents and spread through hand-offs. However, all three analyse completed traces or controlled injections after execution. ConstraintLens adopts their insight that localisation matters, but moves detection to runtime and frames it around explicit constraints rather than overall task failure.

### 2.3 Instruction Drift, Goal Drift and Context Loss

A second line of work measures how adherence to initial instructions decays over interaction length. Li et al. \[9\] use self-chats between two instructed chatbots and observe significant instruction drift within eight rounds, which they connect to attention decay over long exchanges. Arike et al. \[10\] give an agent an explicit goal and then apply competing pressures over contexts exceeding 100,000 tokens, finding that all evaluated models drift to some degree. Menon et al. \[13\] extend this setting and show that models conditioned on trajectories produced by weaker agents often inherit their drift.

Laban et al. \[11\] reveal requirements gradually across turns and report an average 39% performance drop relative to single-turn prompts, mainly due to unreliability once a model commits to early assumptions. Liu et al. \[12\] provide a mechanistic backdrop: models use information at the start and end of long contexts better than information in the middle, which implies that constraints stated early can lose influence as conversations grow.

This evidence motivates ConstraintLens directly. Yet \[9\]–\[12\] study a single agent or a two-party dialog, and \[13\] is the only work found that hints at drift being transmitted from one agent's trajectory to another. None of them offers a runtime detector; they are evaluation methods. ConstraintLens treats drift as a property of the multi-agent message flow and tracks it continuously.

### 2.4 Runtime Verification, Monitoring and Enforcement

Runtime verification (RV) checks an executing system's trace against a specification rather than proving properties of all executions \[14\]. In classical multi-agent systems, RV has been applied to adaptive agent collectives (DAMS-RV \[15\]), to message exchange in JaCaMo through interaction protocols (RV4JaCa \[16\]), and to partially observable systems where some interactions are missed, using probabilistic trace expressions \[17\]. Runtime assurance frameworks such as SOTER \[18\] go further by switching to a safe controller when an untrusted component violates its specification, and Cheng et al. \[19\] monitor neural-network activation patterns to flag unsupported decisions.

For LLM agents, AgentSpec \[20\] introduces a domain-specific language of triggers, predicates and enforcement actions and reports strong safety results with millisecond overhead. ProbGuard \[21\] makes monitoring proactive through probabilistic risk prediction, and Agent-C \[22\] translates temporal policies to first-order logic and checks them with an SMT solver during generation.

These works demonstrate that runtime monitoring of agents is practical. However, their properties are expressed over discrete events, protocol states, tool calls or numeric signals \[15\]–\[22\]. Checking whether the meaning of a natural-language message still satisfies a natural-language constraint lies outside their specification languages. ConstraintLens borrows the RV architecture of monitor, trace and verdict, but replaces the symbolic predicate with a semantic one.

### 2.5 Guardrails, Policy Enforcement and Constraint Compliance

Guardrail toolkits intercept inputs and outputs of an LLM application. NeMo Guardrails \[23\] acts as a proxy runtime and dialog manager, enforcing user-defined rails written in Colang independently of the underlying model. AgentSpec \[20\] and Agent-C \[22\], discussed above, extend this idea to agent actions with explicit enforcement.

On the evaluation side, FollowBench \[24\] decomposes instructions into five categories of fine-grained constraints and adds them one level at a time, finding that all tested models degrade as constraints accumulate. This result is relevant because real multi-agent tasks carry many concurrent constraints such as budgets, formats and policies.

Guardrails are typically scoped to one conversation between a user and a model, and compliance benchmarks are offline and single-turn. Neither tracks whether a constraint is weakened when one agent paraphrases it for another. ConstraintLens complements guardrails: it does not replace enforcement but supplies the cross-agent semantic signal that tells an enforcement layer when to act.

### 2.6 Semantic Consistency Checking and Natural Language Inference

Natural language inference (NLI) offers a practical way to test whether one text entails or contradicts another. SummaC \[25\] shows that NLI becomes competitive for inconsistency detection when documents are split into sentences and pair-wise scores are aggregated, reaching 74.4% balanced accuracy on a six-dataset benchmark. SelfCheckGPT \[26\] detects hallucination by measuring agreement among sampled responses, with its NLI variant offering a good balance of accuracy and cost.

These methods supply the semantic machinery ConstraintLens requires: sentence-level entailment and contradiction scores between a constraint and a message. Their original targets differ, however. SummaC compares a summary with its source and SelfCheckGPT compares a model with itself; neither checks a stream of agent messages against an external, persistent constraint set. ConstraintLens adapts the sentence-level NLI strategy of \[25\] to constraint–message pairs.

### 2.7 Consistency, Message Ordering and State in Asynchronous Systems

In asynchronous distributed systems there is no global clock, so the order in which messages are observed need not reflect causality. Lamport \[27\] defines the happened-before relation and logical clocks to order events consistently, and Raynal and Singhal \[28\] survey scalar, vector and matrix clocks that capture progressively richer causal information.

Recent LLM multi-agent evaluations rarely model asynchrony explicitly. AgentsNet \[29\] borrows classical distributed-systems problems but uses synchronous rounds of message passing, and its authors note that transfer to asynchronous settings is unclear. The turn-based dialogs used in \[9\] and \[11\] are likewise sequential.

For ConstraintLens this distinction is essential. When agents reply out of order, a monitor that compares each message only with the latest preceding one can misattribute drift. Vector-clock style causal metadata \[27\], \[28\] lets ConstraintLens compare a message with the constraint state its sender had actually seen, and trace drift along causal rather than arrival order.

### 2.8 Agent Communication Protocols and Observability

Interoperability protocols are standardising how agents exchange messages. Ehtesham et al. \[30\] compare MCP, ACP, A2A and ANP across interaction modes, discovery, communication patterns and security, and note that ACP supports both synchronous and asynchronous interaction. On the operational side, Dong et al. \[31\] define AgentOps and propose a taxonomy of artifacts to trace across an agent's lifecycle, observing that existing tools focus on LLM metrics rather than agent goals, plans and tools.

These works provide the hooks ConstraintLens needs: structured messages, sessions and spans that can be intercepted. Neither protocols nor observability taxonomies, however, judge whether message content is faithful to earlier constraints. ConstraintLens positions itself as a semantic layer on top of such traces, emitting a drift verdict as an additional span type.

### 2.9 Concept Drift and Specification Drift in ML Systems

Concept drift research studies unforeseeable changes in the distribution of streaming data. Lu et al. \[32\] organise the field into drift detection, drift understanding (when, how and where drift occurs) and drift adaptation, while Ditzler et al. \[33\] contrast active and passive strategies and Iwashita and Papa \[34\] catalogue drift types and benchmark datasets.

The detect–understand–adapt framing of \[32\] maps closely onto ConstraintLens's goals of detecting drift, localising where it began and flagging it for correction. The analogy is conceptual, not technical: concept-drift methods monitor statistical distributions of features and labels, whereas semantic constraint drift is a change in the meaning of instructions carried by individual messages. No surveyed concept-drift work addresses this semantic, multi-agent variant.

## Part 3: Limitations Identified from Literature Survey (Research Gaps)

Ten gaps are supported by the surveyed literature. Each maps to one or more ConstraintLens capabilities (Detect, Localize, Compose, Benchmark, Enforce) and is claimed only as far as the cited works show it.

| # | Gap | Evidence | ConstraintLens response |
| --- | --- | --- | --- |
| 1 | Structure checked, meaning not | \[40\], \[41\], \[43\], \[44\], \[45\], \[16\], \[20\], \[22\] | Semantic tiers 1b, 1c and 2 at every hand-off |
| 2 | Checked once at the start | \[37\], \[44\], \[45\], \[24\] | Checks at PUBLISH, CONSUME and TOOL\_CALL on every hop |
| 3 | Drift named but not built or measured multi-agent | \[35\], \[36\], \[38\], \[39\], \[10\], \[13\] | Working service plus driftchain-bench |
| 4 | Text drift and action harm not linked | \[36\], \[38\], \[39\], \[20\], \[22\] | Separate textual-drift and action-violation verdicts; mandatory tool-call gate |
| 5 | Asynchronous conditions ignored | \[29\], \[9\], \[11\], \[36\], \[30\], \[27\], \[28\] | Async guard for staleness, reordering, gaps and duplicates |
| 6 | No localisation of the hop that changed meaning | \[6\], \[7\], \[40\] | Hop-by-hop score graph with cause labels |
| 7 | Multiple constraints not composed semantically | \[24\], \[41\], \[45\] | Strictest-wins composition; silent-drop detection |
| 8 | Semantic checkers weak on numbers and negation; judges manipulable | \[50\], \[51\], \[36\], \[49\], \[52\] | Deterministic tiers first; embeddings route only; judge only in the uncertainty band |
| 9 | Injection studied per agent, not as a drift cause across hops | \[46\], \[47\], \[48\] | Immutable envelope separate from task text; INJECTION as an attributed cause |
| 10 | No labelled benchmark of asynchronous hand-off chains | \[36\], \[6\], \[7\], \[47\], \[24\], \[29\] | driftchain-bench with mutation operators and meaning-preserving controls |

### Gap 1: Existing safeguards check structure, not whether meaning survives hand-offs

**What is missing.** Delegation and authorization schemes verify who delegated, which scope was granted and whether tool arguments stay inside typed limits. None checks whether a reworded natural-language task still implies the original constraint.

**Evidence.** HDP records signed delegation hops and states it is not an authorization protocol \[40\]. Attenuating tokens constrain tool names and arguments \[41\], and Vouch signs agent identity and actions \[43\]. Intent-certificate authorization \[44\] and authenticated delegation \[45\] fix scope from the initial request. Runtime enforcement tools check protocol states, actions or formal temporal properties \[16\], \[20\], \[22\].

**How ConstraintLens addresses it.** It keeps these structural checks as Tier 0 and Tier 1a, then adds Tiers 1b, 1c and 2 to test whether the task text at each hop still implies each constraint.

### Gap 2: Constraints are checked once at the start, not at every hop

**What is missing.** Several approaches evaluate or fix the constraint when the request first arrives and assume it holds thereafter.

**Evidence.** ASTRA evaluates adherence to guardrails written once in each agent's system prompt \[37\]. IGAC derives its intent certificate once from the trusted request \[44\], and authenticated delegation fixes scope at delegation time \[45\]. Constraint-following benchmarks are single-turn \[24\].

**How ConstraintLens addresses it.** The SDK calls the service at three checkpoints on every hop (PUBLISH, CONSUME and TOOL\_CALL), so the constraint is re-checked whenever the task changes hands or is about to act.

### Gap 3: Constraint drift is named but not implemented or measured in multi-agent settings

**What is missing.** Recent work names constraint and safety drift, but either offers no system or studies one model or one agent.

**Evidence.** The Liverpool position paper formalises constraint drift but has no implementation \[35\]. DRIFTBENCH measures drift in one model across conversation turns \[36\]. Safety drift is characterised in single-agent case studies \[38\] and trajectories \[39\], and goal drift in single agents \[10\], \[13\].

**How ConstraintLens addresses it.** It provides a working runtime service for multi-agent pipelines and evaluates it on driftchain-bench, turning the position in \[35\] into a measurable system.

### Gap 4: Textual drift and action harm are not distinguished or linked

**What is missing.** Monitors look either at what agents say or at what tools do. Evidence shows the two can diverge, so checking only one misses failures.

**Evidence.** Models restate constraints they are violating, with knows-but-violates rates up to 99% \[36\]. Declared safety intent can erode into violating actions \[38\], and individually safe steps can compose into a violation \[39\]. Action-level enforcers \[20\], \[22\] see the tool call but not the earlier hop where the constraint was lost.

**How ConstraintLens addresses it.** It reports textual drift as an early warning and action violation as the harm, links them through the trace, and makes the TOOL\_CALL gate mandatory so harm is blocked even when text checks miss.

### Gap 5: Asynchronous delivery conditions are ignored

**What is missing.** Drift and coordination studies assume turn-by-turn or synchronous exchange. Queue delays, stale constraints, out-of-order or missing messages, and duplicate deliveries are not modelled.

**Evidence.** AgentsNet uses synchronous rounds and notes that transfer to asynchronous settings is unclear \[29\]. Drift studies are turn-based \[9\], \[11\], \[36\]. Agent protocols already support asynchronous interaction \[30\], and causal ordering requires logical-clock reasoning \[27\], \[28\].

**How ConstraintLens addresses it.** The async guard flags constraints that have expired by pick-up time, detects reordered or missing hops, and keys verdicts by (trace\_id, hop\_id, checkpoint) so duplicate deliveries receive identical answers.

### Gap 6: The hop and agent that introduced drift are not localised

**What is missing.** When an agent pipeline goes wrong, existing methods attribute task failure after the run or record who delegated, but do not identify which hop changed a constraint's meaning or why.

**Evidence.** Automated failure attribution is hard even for strong reasoning models and runs post hoc \[7\]. MAST labels failure modes but not originating messages \[6\]. HDP records each delegation hop but not whether meaning changed at that hop \[40\].

**How ConstraintLens addresses it.** It treats the chain as a graph, tracks each constraint's score hop by hop, names the first hop where it fell, and labels the cause as REWRITE, STALE\_CONTEXT, TRANSIT\_LOSS, INJECTION or UNKNOWN.

### Gap 7: Multiple overlapping constraints are not composed semantically

**What is missing.** Real tasks carry user, organisation and system constraints at once. Composition exists for structured scopes, but not for constraints that live in natural-language task text, and silent loss of one of several constraints is not tracked.

**Evidence.** Constraint following degrades as the number of constraints rises \[24\]. Attenuating tokens enforce that each holder only narrows tool and argument scope \[41\], and authenticated delegation propagates scope limits between agents \[45\], but both operate on structured permissions.

**How ConstraintLens addresses it.** It composes constraints by strictest-wins rules (lowest limit, narrowest scope, earliest expiry, approval if any requires it) and flags a hop where one constraint silently disappears while others remain.

### Gap 8: Semantic checkers fail on numbers and negation, and LLM judges can be manipulated

**What is missing.** A purely neural checker is unreliable for exactly the edits that matter in constraint drift, such as changed amounts, dropped qualifiers and flipped negations. A single LLM judge is costly and attackable.

**Evidence.** NLI models mislabel statements containing negation and fail numerical reasoning \[50\], and on quantitative entailment do not beat a majority baseline on average \[51\]. An LLM judge under-detected constraint violations relative to blind human raters \[36\]. Optimised injections steer LLM judges and evade known defenses \[49\]. Model cascades cut cost \[52\] but target accuracy, not fail-closed safety decisions.

**How ConstraintLens addresses it.** It runs deterministic checks first (Tier 1a argument comparison, Tier 1b extraction of numbers, qualifiers and negations), uses embeddings only to route, keeps the stricter value when extractors disagree, and calls the cached judge only inside the uncertainty band, with judge manipulation in its threat model.

### Gap 9: Prompt injection is studied within one agent, not as a drift cause across hops

**What is missing.** Injection research focuses on untrusted tool output hijacking a single agent. How injected instructions alter constraints as tasks pass between agents, and how to attribute that, is not covered.

**Evidence.** Indirect injection is demonstrated against single LLM-integrated applications \[46\]. AgentDojo benchmarks single agents \[47\]. CaMeL separates trusted control flow from untrusted data within one agent's plan \[48\].

**How ConstraintLens addresses it.** Constraint metadata travels in an envelope separate from the task text that agents cannot modify, with the master copy held by the service, and INJECTION is one of the attributed drift causes.

### Gap 10: No labelled benchmark of asynchronous hand-off chains with meaning-preserving controls

**What is missing.** Available datasets label single-model drift, task-level failures or injection outcomes. None provides multi-hop asynchronous chains with a known mutation, a known hop of origin, and controls that are heavily reworded but still faithful, which are needed to measure false blocks.

**Evidence.** DRIFTBENCH covers single-model ideation \[36\]. MAST-Data and Who&When label task failures \[6\], \[7\]. AgentDojo labels injection outcomes \[47\]. FollowBench is single-turn \[24\], and AgentsNet is synchronous \[29\].

**How ConstraintLens addresses it.** driftchain-bench generates chains from six scenarios and nine mutation operators across hop positions, topologies and async profiles, with about half controls and at least 300 chains labelled blind by hand.

## Part 4: Summary

The literature shows that LLM-based multi-agent frameworks are mature enough to coordinate complex work \[1\]–\[5\], yet they fail frequently, with failures originating at specific agents and spreading through inter-agent hand-offs \[6\]–\[8\]. Independent studies confirm that adherence to initial instructions and goals decays over long or multi-turn interactions \[9\]–\[12\], and early evidence suggests such drift can be inherited from other agents' trajectories \[13\]. Runtime verification and guardrail research demonstrates that agents can be monitored and constrained at low overhead \[14\]–\[23\], but these tools express properties over events, protocols, actions or numeric signals rather than over the meaning of natural-language messages. Semantic consistency methods based on NLI \[25\], \[26\] provide the missing scoring capability, causal-ordering theory \[27\], \[28\] provides the tools to reason about asynchronous message flow, and observability and protocol work \[30\], \[31\] provides the interception points, while concept-drift research \[32\]–\[34\] offers a detect–understand–adapt framing. No surveyed work combines these elements into a runtime monitor that detects and localises semantic constraint drift across asynchronous agent hand-offs. This gap is the motivation for ConstraintLens.

## Part 5: References

References follow IEEE style and are numbered as cited. Entries marked \[abstract only\] were assessed from the abstract or publisher landing page.

&#91;1\] Q. Wu, G. Bansal, J. Zhang, Y. Wu, S. Zhang, E. Zhu, B. Li, L. Jiang, X. Zhang, and C. Wang, "AutoGen: Enabling next-gen LLM applications via multi-agent conversation framework," arXiv preprint arXiv:2308.08155, 2023. \[Online\]. Available: https://arxiv.org/abs/2308.08155

&#91;2\] S. Hong, M. Zhuge, J. Chen, X. Zheng, Y. Cheng, C. Zhang, J. Wang, Z. Wang, S. K. S. Yau, Z. Lin, L. Zhou, C. Ran, L. Xiao, C. Wu, and J. Schmidhuber, "MetaGPT: Meta programming for a multi-agent collaborative framework," in Proc. Int. Conf. Learn. Represent. (ICLR), 2024. \[Online\]. Available: https://proceedings.iclr.cc/paper\_files/paper/2024/hash/6507b115562bb0a305f1958ccc87355a-Abstract-Conference.html

&#91;3\] G. Li, H. Hammoud, H. Itani, D. Khizbullin, and B. Ghanem, "CAMEL: Communicative agents for 'mind' exploration of large language model society," in Adv. Neural Inf. Process. Syst. (NeurIPS), vol. 36, 2023. \[Online\]. Available: https://proceedings.neurips.cc/paper/2023/hash/a3621ee907def47c1b952ade25c67698-Abstract-Conference.html

&#91;4\] T. Guo, X. Chen, Y. Wang, R. Chang, S. Pei, N. V. Chawla, O. Wiest, and X. Zhang, "Large language model based multi-agents: A survey of progress and challenges," in Proc. 33rd Int. Joint Conf. Artif. Intell. (IJCAI), 2024, pp. 8048–8057, doi: 10.24963/ijcai.2024/890.

&#91;5\] N. Nascimento, P. Alencar, and D. Cowan, "Self-adaptive large language model (LLM)-based multiagent systems," in Proc. IEEE Int. Conf. Autonomic Comput. Self-Organizing Syst. Companion (ACSOS-C), 2023, pp. 104–109. \[Online\]. Available: https://arxiv.org/abs/2307.06187

&#91;6\] M. Cemri, M. Z. Pan, S. Yang, L. A. Agrawal, B. Chopra, R. Tiwari, K. Keutzer, A. Parameswaran, D. Klein, K. Ramchandran, M. Zaharia, J. E. Gonzalez, and I. Stoica, "Why do multi-agent LLM systems fail?," in Adv. Neural Inf. Process. Syst. (NeurIPS), 2025, doi: 10.48550/arXiv.2503.13657.

&#91;7\] S. Zhang, M. Yin, J. Zhang, J. Liu, Z. Han, J. Zhang, B. Li, C. Wang, et al., "Which agent causes task failures and when? On automated failure attribution of LLM multi-agent systems," in Proc. 42nd Int. Conf. Mach. Learn. (ICML), PMLR, vol. 267, 2025, pp. 76583–76599. \[Online\]. Available: https://arxiv.org/abs/2505.00212

&#91;8\] J. Huang, J. Zhou, T. Jin, X. Zhou, Z. Chen, W. Wang, Y. Yuan, M. R. Lyu, and M. Sap, "On the resilience of LLM-based multi-agent collaboration with faulty agents," in Proc. 42nd Int. Conf. Mach. Learn. (ICML), PMLR, vol. 267, 2025, pp. 26202–26226. \[Online\]. Available: https://proceedings.mlr.press/v267/huang25ay.html

&#91;9\] K. Li, T. Liu, N. Bashkansky, D. Bau, F. Viégas, H. Pfister, and M. Wattenberg, "Measuring and controlling instruction (in)stability in language model dialogs," in Proc. Conf. Lang. Model. (COLM), 2024, doi: 10.48550/arXiv.2402.10962.

&#91;10\] R. Arike, E. Donoway, H. Bartsch, and M. Hobbhahn, "Evaluating goal drift in language model agents," in Proc. AAAI/ACM Conf. AI, Ethics, Soc. (AIES), vol. 8, no. 1, 2025, pp. 192–203, doi: 10.1609/aies.v8i1.36541.

&#91;11\] P. Laban, H. Hayashi, Y. Zhou, and J. Neville, "LLMs get lost in multi-turn conversation," arXiv preprint arXiv:2505.06120, 2025 (listed as ICLR 2026), doi: 10.48550/arXiv.2505.06120.

&#91;12\] N. F. Liu, K. Lin, J. Hewitt, A. Paranjape, M. Bevilacqua, F. Petroni, and P. Liang, "Lost in the middle: How language models use long contexts," Trans. Assoc. Comput. Linguist., vol. 12, pp. 157–173, 2024, doi: 10.1162/tacl\_a\_00638.

&#91;13\] A. Menon, M. Saebo, T. Crosse, S. Gibson, E. Jang, and D. Cruz, "Inherited goal drift: Contextual pressure can undermine agentic goals," arXiv preprint arXiv:2603.03258, 2026 (ICLR 2026 Lifelong Agents Workshop). \[Online\]. Available: https://arxiv.org/abs/2603.03258

&#91;14\] M. Leucker and C. Schallhart, "A brief account of runtime verification," J. Log. Algebraic Program., vol. 78, no. 5, pp. 293–303, 2009, doi: 10.1016/j.jlap.2008.08.004. \[abstract only\]

&#91;15\] Y. J. Lim, G. Hong, D. Shin, E. Jee, and D.-H. Bae, "A runtime verification framework for dynamically adaptive multi-agent systems," in Proc. Int. Conf. Big Data Smart Comput. (BigComp), IEEE, 2016, pp. 509–512, doi: 10.1109/BIGCOMP.2016.7425981. \[abstract only\]

&#91;16\] D. C. Engelmann, A. Ferrando, A. R. Panisson, D. Ancona, R. H. Bordini, and V. Mascardi, "RV4JaCa – Runtime verification for multi-agent systems," in Proc. 2nd Workshop Agents Robots Reliable Eng. Autonomy (AREA), EPTCS, vol. 362, 2022, pp. 23–36, doi: 10.4204/EPTCS.362.5.

&#91;17\] D. Ancona, A. Ferrando, and V. Mascardi, "Mind the gap! Runtime verification of partially observable MASs with probabilistic trace expressions," in Proc. 19th Eur. Conf. Multi-Agent Syst. (EUMAS), LNCS, vol. 13442, Springer, 2022, pp. 22–40, doi: 10.1007/978-3-031-20614-6\_2. \[abstract only\]

&#91;18\] A. Desai, S. Ghosh, S. A. Seshia, N. Shankar, and A. Tiwari, "SOTER: A runtime assurance framework for programming safe robotics systems," in Proc. IEEE/IFIP Int. Conf. Dependable Syst. Netw. (DSN), 2019. \[Online\]. Available: https://arxiv.org/abs/1808.07921

&#91;19\] C.-H. Cheng, G. Nührenberg, and H. Yasuoka, "Runtime monitoring neuron activation patterns," in Proc. Design, Autom. Test Eur. Conf. (DATE), 2019. \[Online\]. Available: https://past.date-conference.com/proceedings-archive/2019/html/0290.html

&#91;20\] H. Wang, C. M. Poskitt, and J. Sun, "AgentSpec: Customizable runtime enforcement for safe and reliable LLM agents," in Proc. IEEE/ACM 48th Int. Conf. Softw. Eng. (ICSE), 2026, doi: 10.48550/arXiv.2503.18666.

&#91;21\] H. Wang, C. M. Poskitt, J. Wei, and J. Sun, "ProbGuard: Probabilistic runtime monitoring for LLM agent safety," arXiv preprint arXiv:2508.00500, 2025, doi: 10.48550/arXiv.2508.00500. \[abstract only\]

&#91;22\] A. Kamath, S. Zhang, C. Xu, S. Ugare, G. Singh, and S. Misailovic, "Enforcing temporal constraints for LLM agents," arXiv preprint arXiv:2512.23738, 2025, doi: 10.48550/arXiv.2512.23738.

&#91;23\] T. Rebedea, R. Dinu, M. N. Sreedhar, C. Parisien, and J. Cohen, "NeMo Guardrails: A toolkit for controllable and safe LLM applications with programmable rails," in Proc. Conf. Empirical Methods Natural Lang. Process.: Syst. Demonstrations (EMNLP), 2023, pp. 431–445, doi: 10.18653/v1/2023.emnlp-demo.40.

&#91;24\] Y. Jiang, Y. Wang, X. Zeng, W. Zhong, L. Li, F. Mi, L. Shang, X. Jiang, Q. Liu, and W. Wang, "FollowBench: A multi-level fine-grained constraints following benchmark for large language models," in Proc. 62nd Annu. Meeting Assoc. Comput. Linguist. (ACL), 2024, pp. 4667–4688, doi: 10.18653/v1/2024.acl-long.257.

&#91;25\] P. Laban, T. Schnabel, P. N. Bennett, and M. A. Hearst, "SummaC: Re-visiting NLI-based models for inconsistency detection in summarization," Trans. Assoc. Comput. Linguist., vol. 10, pp. 163–177, 2022, doi: 10.1162/tacl\_a\_00453.

&#91;26\] P. Manakul, A. Liusie, and M. J. F. Gales, "SelfCheckGPT: Zero-resource black-box hallucination detection for generative large language models," in Proc. Conf. Empirical Methods Natural Lang. Process. (EMNLP), 2023, pp. 9004–9017, doi: 10.18653/v1/2023.emnlp-main.557.

&#91;27\] L. Lamport, "Time, clocks, and the ordering of events in a distributed system," Commun. ACM, vol. 21, no. 7, pp. 558–565, 1978, doi: 10.1145/359545.359563.

&#91;28\] M. Raynal and M. Singhal, "Logical time: Capturing causality in distributed systems," IEEE Computer, vol. 29, no. 2, pp. 49–56, Feb. 1996. \[Online\]. Available: https://decomposition.al/CSE232-2021-09/readings/extra/raynal-singhal-logical-time-survey.pdf

&#91;29\] F. Grötschla, L. Müller, J. Tönshoff, M. Galkin, and B. Perozzi, "AgentsNet: Coordination and collaborative reasoning in multi-agent LLMs," arXiv preprint arXiv:2507.08616, 2025, doi: 10.48550/arXiv.2507.08616.

&#91;30\] A. Ehtesham, A. Singh, G. K. Gupta, and S. Kumar, "A survey of agent interoperability protocols: Model Context Protocol (MCP), Agent Communication Protocol (ACP), Agent-to-Agent Protocol (A2A), and Agent Network Protocol (ANP)," arXiv preprint arXiv:2505.02279, 2025, doi: 10.48550/arXiv.2505.02279.

&#91;31\] L. Dong, Q. Lu, and L. Zhu, "AgentOps: Enabling observability of LLM agents," arXiv preprint arXiv:2411.05285, 2024, doi: 10.48550/arXiv.2411.05285.

&#91;32\] J. Lu, A. Liu, F. Dong, F. Gu, J. Gama, and G. Zhang, "Learning under concept drift: A review," IEEE Trans. Knowl. Data Eng., vol. 31, no. 12, pp. 2346–2363, 2019, doi: 10.1109/TKDE.2018.2876857.

&#91;33\] G. Ditzler, M. Roveri, C. Alippi, and R. Polikar, "Learning in nonstationary environments: A survey," IEEE Comput. Intell. Mag., vol. 10, no. 4, pp. 12–25, Nov. 2015, doi: 10.1109/MCI.2015.2471196. \[abstract only\]

&#91;34\] A. S. Iwashita and J. P. Papa, "An overview on concept drift learning," IEEE Access, vol. 7, pp. 1532–1547, 2019, doi: 10.1109/ACCESS.2018.2886026. \[abstract only\]

&#91;35\] T. Li, Y. Ma, H. Wen, Z. Huang, Q. Zhou, Z. Fu, and G. Cheng, "Safe multi-agent behavior must be maintained, not merely asserted: Constraint drift in LLM-based multi-agent systems," arXiv preprint arXiv:2605.10481, 2026. \[Online\]. Available: https://arxiv.org/abs/2605.10481

&#91;36\] G. Kruthof, "Models recall what they violate: Constraint adherence in multi-turn LLM ideation," arXiv preprint arXiv:2604.28031, 2026 (listed as NeurIPS 2026 poster). \[Online\]. Available: https://arxiv.org/abs/2604.28031

&#91;37\] I. Hazan, Y. Mathov, G. Shtar, R. Bitton, and I. Mantin, "ASTRA: Agentic steerability and risk assessment framework," arXiv preprint arXiv:2511.18114, 2025, doi: 10.48550/arXiv.2511.18114. \[abstract only\]

&#91;38\] S. Yu, F. Carroll, and B. L. Bentley, "Operational hallucination and safety drift in AI agents," in Proc. IEEE Int. Conf. AI Data Analytics (ICAD), 2026 (accepted manuscript). \[Online\]. Available: https://arxiv.org/abs/2607.18366

&#91;39\] A. Dhodapkar and F. Pishori, "SafetyDrift: Predicting when AI agents cross the line before they actually do," arXiv preprint arXiv:2603.27148, 2026. \[Online\]. Available: https://arxiv.org/abs/2603.27148

&#91;40\] A. Dalugoda, "Human Delegation Provenance Protocol (HDP): Cryptographic chain-of-custody for agentic AI systems," IETF Internet-Draft draft-helixar-hdp-agentic-delegation-00, Mar. 2026 (work in progress). \[Online\]. Available: https://datatracker.ietf.org/doc/html/draft-helixar-hdp-agentic-delegation-00

&#91;41\] N. A. Niyikiza, "Attenuating authorization tokens for agentic delegation chains," IETF Internet-Draft draft-niyikiza-oauth-attenuating-agent-tokens-00, Mar. 2026 (work in progress). \[Online\]. Available: https://www.ietf.org/archive/id/draft-niyikiza-oauth-attenuating-agent-tokens-00.txt

&#91;42\] K. Tallam, "A five-plane reference architecture for runtime governance of production AI agents," arXiv preprint arXiv:2606.12320, 2026. \[Online\]. Available: https://arxiv.org/abs/2606.12320 \[abstract only\]

&#91;43\] Vouch Protocol, "Vouch Protocol: The open standard for AI agent identity and accountability," open specification and source code, 2025–2026. \[Online\]. Available: https://github.com/vouch-protocol/vouch (grey literature)

&#91;44\] G. Zhu and C. Wang, "Intent-governed tool authorization for AI agents," arXiv preprint arXiv:2606.22916, 2026. \[Online\]. Available: https://arxiv.org/abs/2606.22916

&#91;45\] T. South, S. Marro, T. Hardjono, R. Mahari, C. D. Whitney, A. Chan, and A. Pentland, "Position: AI agents need authenticated delegation," in Proc. 42nd Int. Conf. Mach. Learn. (ICML), PMLR, vol. 267, 2025, pp. 82211–82231. \[Online\]. Available: https://proceedings.mlr.press/v267/south25a.html

&#91;46\] K. Greshake, S. Abdelnabi, S. Mishra, C. Endres, T. Holz, and M. Fritz, "Not what you've signed up for: Compromising real-world LLM-integrated applications with indirect prompt injection," in Proc. 16th ACM Workshop Artif. Intell. Secur. (AISec), 2023, pp. 79–90, doi: 10.48550/arXiv.2302.12173.

&#91;47\] E. Debenedetti, J. Zhang, M. Balunović, L. Beurer-Kellner, M. Fischer, and F. Tramèr, "AgentDojo: A dynamic environment to evaluate prompt injection attacks and defenses for LLM agents," in Adv. Neural Inf. Process. Syst. (NeurIPS) Datasets and Benchmarks Track, vol. 37, 2024, doi: 10.52202/079017-2636.

&#91;48\] E. Debenedetti, I. Shumailov, T. Fan, J. Hayes, N. Carlini, D. Fabian, C. Kern, C. Shi, A. Terzis, and F. Tramèr, "Defeating prompt injections by design," arXiv preprint arXiv:2503.18813, 2025, doi: 10.48550/arXiv.2503.18813.

&#91;49\] J. Shi, Z. Yuan, Y. Liu, Y. Huang, P. Zhou, L. Sun, and N. Z. Gong, "Optimization-based prompt injection attack to LLM-as-a-judge," in Proc. ACM SIGSAC Conf. Comput. Commun. Secur. (CCS), 2024, doi: 10.1145/3658644.3690291.

&#91;50\] A. Naik, A. Ravichander, N. Sadeh, C. Rose, and G. Neubig, "Stress test evaluation for natural language inference," in Proc. 27th Int. Conf. Comput. Linguist. (COLING), 2018, pp. 2340–2353. \[Online\]. Available: https://aclanthology.org/C18-1198

&#91;51\] A. Ravichander, A. Naik, C. Rose, and E. Hovy, "EQUATE: A benchmark evaluation framework for quantitative reasoning in natural language inference," in Proc. 23rd Conf. Comput. Natural Lang. Learn. (CoNLL), 2019, pp. 349–361, doi: 10.18653/v1/K19-1033.

&#91;52\] L. Chen, M. Zaharia, and J. Zou, "FrugalGPT: How to use large language models while reducing cost and improving performance," Trans. Mach. Learn. Res. (TMLR), 2024. \[Online\]. Available: https://mlanthology.org/tmlr/2024/chen2024tmlr-frugalgpt/

### Verification notes

- Venues for \[6\] (NeurIPS 2025) and \[11\] (ICLR 2026) were confirmed through proceedings listings and conference pages rather than the final camera-ready PDFs; cite the arXiv versions if your guide requires a fetched final version.
- DOIs for \[5\], \[18\], \[19\] and \[28\] were not confirmed during the search, so stable URLs are given instead. Add the IEEE Xplore DOIs from the publisher pages before submission.
- &#91;19\] is not counted toward the IEEE total because its publisher could not be confirmed in this search.
- The AgentsNet limitation on asynchronous settings \[29\] was taken from a summary of the paper's limitations section, not read directly.
