// 基于 SSE 的研究流 hook
// 与 @langchain/langgraph-sdk 的 useStream 并存，用于异步任务场景

import { useState, useRef, useCallback, useEffect } from "react";
import { fetchResearchHistory, type ResearchHistoryItem } from "@/lib/api";

const API_BASE_URL = "";

export interface ResearchMessage {
  type: "human" | "ai";
  content: string;
  id: string;
}

export interface ResearchSource {
  label?: string;
  url?: string;
  title?: string;
  [key: string]: unknown;
}

interface ResearchEvent {
  generate_plan?: { plan: string };
  generate_query?: { search_query: string[] };
  web_research?: { sources_gathered: ResearchSource[] };
  reflection?: Record<string, unknown>;
  finalize_answer?: { messages?: Array<{ content?: string }> };
  task_paused?: boolean;
  task_cancelled?: boolean;
  token?: { text: string; node: string };
  error?: string;
}

interface UseResearchStreamReturn {
  events: ResearchEvent[];
  messages: ResearchMessage[];
  history: ResearchHistoryItem[];
  activeTaskId: string;
  plan: string;
  awaitingPlanConfirmation: boolean;
  isLoading: boolean;
  isHistoryLoading: boolean;
  /** 当前正在流式输出的节点名称（null 表示非流式状态） */
  streamingNode: string | null;
  /** 当前节点已流式输出的累计文本 */
  streamingContent: string;
  submit: (input: string, effort: string, model: string, extra?: { plan?: string; planStatus?: string }) => Promise<void>;
  refreshHistory: () => Promise<void>;
  restoreHistoryItem: (item: ResearchHistoryItem) => void;
  startNewResearch: () => void;
  stop: () => void;
}

export function useResearchStream(): UseResearchStreamReturn {
  const [events, setEvents] = useState<ResearchEvent[]>([]);
  const [messages, setMessages] = useState<ResearchMessage[]>([]);
  const [plan, setPlan] = useState("");
  const [awaitingPlanConfirmation, setAwaitingPlanConfirmation] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const [history, setHistory] = useState<ResearchHistoryItem[]>([]);
  const [activeTaskId, setActiveTaskId] = useState("");
  const [streamingNode, setStreamingNode] = useState<string | null>(null);
  const [streamingContent, setStreamingContent] = useState("");
  const eventSourceRef = useRef<EventSource | null>(null);
  const messageIdCounter = useRef(0);
  const planRef = useRef("");  // 缓存 generate_plan 中的计划内容
  const taskIdRef = useRef<string>("");  // 保存首次任务响应的 task_id，后续提交复用
  const loadedHistoryRef = useRef(false);

  // 流式状态 refs（避免闭包过期问题）
  const streamingNodeRef = useRef<string | null>(null);
  const streamingContentRef = useRef("");

  /** 重置流式状态 */
  const _resetStreaming = useCallback(() => {
    streamingNodeRef.current = null;
    streamingContentRef.current = "";
    setStreamingNode(null);
    setStreamingContent("");
  }, []);

  const buildMessagesFromHistoryItem = useCallback((item: ResearchHistoryItem): ResearchMessage[] => {
    const restoredMessages: ResearchMessage[] = [
      {
        type: "human",
        content: item.title,
        id: String(++messageIdCounter.current),
      },
    ];

    if (item.report) {
      restoredMessages.push({
        type: "ai",
        content: item.report,
        id: String(++messageIdCounter.current),
      });
    } else if (item.status) {
      restoredMessages.push({
        type: "ai",
        content: `已恢复历史任务，当前状态：${item.status}`,
        id: String(++messageIdCounter.current),
      });
    }

    return restoredMessages;
  }, []);

  const stop = useCallback((cancelBackend = true) => {
    const taskId = taskIdRef.current;
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
    if (cancelBackend && taskId) {
      void fetch(`${API_BASE_URL}/api/research/${taskId}/cancel`, {
        method: "POST",
        credentials: "include",
      });
    }
    setIsLoading(false);
    setAwaitingPlanConfirmation(false);
    _resetStreaming();
  }, [_resetStreaming]);

  const refreshHistory = useCallback(async () => {
    setIsHistoryLoading(true);
    try {
      const items = await fetchResearchHistory(20);
      setHistory(items);
    } finally {
      setIsHistoryLoading(false);
    }
  }, []);

  const restoreHistoryItem = useCallback((item: ResearchHistoryItem) => {
    stop(false);
    taskIdRef.current = item.task_id;
    setActiveTaskId(item.task_id);
    planRef.current = "";
    setPlan("");
    setEvents([]);
    setAwaitingPlanConfirmation(false);
    _resetStreaming();
    setMessages(buildMessagesFromHistoryItem(item));
  }, [buildMessagesFromHistoryItem, _resetStreaming, stop]);

  const startNewResearch = useCallback(() => {
    stop(false);
    taskIdRef.current = "";
    setActiveTaskId("");
    planRef.current = "";
    setPlan("");
    setEvents([]);
    setMessages([]);
    setAwaitingPlanConfirmation(false);
    _resetStreaming();
  }, [_resetStreaming, stop]);

  useEffect(() => {
    if (loadedHistoryRef.current) return;
    loadedHistoryRef.current = true;

    fetchResearchHistory(20)
      .then((items) => {
        setHistory(items);
        const latest = items[0];
        if (!latest) return;
        taskIdRef.current = latest.task_id;
        setActiveTaskId(latest.task_id);
        setMessages(buildMessagesFromHistoryItem(latest));
      })
      .catch((err: unknown) => {
        console.warn("恢复历史记录失败:", err);
      });
  }, [buildMessagesFromHistoryItem]);

  const submit = useCallback(
    async (input: string, effort: string, model: string, extra?: { plan?: string; planStatus?: string }) => {
      if (!input.trim()) return;

      // 停止之前的连接
      stop(false);

      setIsLoading(true);
      setAwaitingPlanConfirmation(false);
      setEvents([]);
      _resetStreaming();

      // 首次提交时清空 taskIdRef，让后端生成新 task_id
      const isFirstSubmit = messages.length === 0;
      if (isFirstSubmit) {
        taskIdRef.current = "";
        planRef.current = "";
        setPlan("");
      }

      const humanMsg: ResearchMessage = {
        type: "human",
        content: input,
        id: String(++messageIdCounter.current),
      };
      setMessages(prev => [...prev, humanMsg]);

      let initial_search_query_count: number;
      let max_research_loops: number;
      switch (effort) {
        case "low":
          initial_search_query_count = 1;
          max_research_loops = 1;
          break;
        case "medium":
        default:
          initial_search_query_count = 3;
          max_research_loops = 3;
          break;
        case "high":
          initial_search_query_count = 5;
          max_research_loops = 5;
          break;
      }

      try {
        // 构建请求体（包含完整对话历史和 plan 状态）
        const body: {
          messages: ResearchMessage[];
          initial_search_query_count: number;
          max_research_loops: number;
          reasoning_model: string;
          plan?: string;
          plan_status?: string;
          task_id?: string;
        } = {
          messages: [...messages, humanMsg],
          initial_search_query_count,
          max_research_loops,
          reasoning_model: model,
        };
        if (extra?.plan) {
          body.plan = extra.plan;
          body.plan_status = extra.planStatus || "confirmed";
        }
        // 后续提交回传同一个 task_id，使后端复用 LangGraph checkpoint
        if (taskIdRef.current) {
          body.task_id = taskIdRef.current;
        }

        // 1. 提交任务，立即拿到 task_id
        const res = await fetch(`${API_BASE_URL}/api/research`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify(body),
        });

        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.error || `HTTP ${res.status}`);
        }

        const { stream_url, task_id } = await res.json();

        // 保存 task_id 供后续提交复用
        if (task_id) {
          taskIdRef.current = task_id;
          setActiveTaskId(task_id);
          void refreshHistory();
        }

        // 2. 建立 SSE 连接接收事件
        const es = new EventSource(`${API_BASE_URL}${stream_url}`);
        eventSourceRef.current = es;

        es.onmessage = (e) => {
          try {
            const raw = JSON.parse(e.data);

            // ── 处理 token 流式事件 ──────────────────────────
            if (raw.token) {
              const { text, node } = raw.token as { text: string; node: string };
              if (node !== streamingNodeRef.current) {
                // 新节点开始流式
                streamingNodeRef.current = node;
                streamingContentRef.current = text;
                setStreamingNode(node);
                setStreamingContent(text);
              } else {
                // 同一节点继续追加
                streamingContentRef.current += text;
                setStreamingContent(prev => prev + text);
              }
              return; // token 事件不需要进一步处理
            }

            const event: ResearchEvent = raw;

            if (event.error) {
              setIsLoading(false);
              setAwaitingPlanConfirmation(false);
              setEvents(prev => [...prev, event]);
              _resetStreaming();
              es.close();
              return;
            }

            if (event.task_cancelled) {
              setIsLoading(false);
              setAwaitingPlanConfirmation(false);
              setEvents(prev => [...prev, event]);
              _resetStreaming();
              es.close();
              return;
            }

            setEvents(prev => [...prev, event]);

            // 节点完成事件到达 → 清空对应节点的流式 buffer
            if (event.generate_plan) {
              planRef.current = event.generate_plan.plan || "";
              setPlan(planRef.current);
              if (streamingNodeRef.current === "generate_plan") {
                _resetStreaming();
              }
            }

            // finalize_answer 时结束
            if (event.finalize_answer) {
              setIsLoading(false);
              setAwaitingPlanConfirmation(false);
              // 使用后端 Post.extract_pattern 清洗后的内容（不含 markdown fence）
              const finalContent = event.finalize_answer?.messages?.[0]?.content || "";
              setMessages(prev => [
                ...prev,
                {
                  type: "ai",
                  content: finalContent,
                  id: String(++messageIdCounter.current),
                },
              ]);
              _resetStreaming();
              void refreshHistory();
              es.close();
            }

            // task_paused 时结束（等待 Plan 确认，后续继续用本 hook 提交确认）
            if (event.task_paused) {
              setIsLoading(false);
              setAwaitingPlanConfirmation(true);
              _resetStreaming();
              // 从缓存的 generate_plan 事件中获取计划内容
              const planContent = planRef.current;
              setMessages(prev => [
                ...prev,
                {
                  type: "ai",
                  content: planContent,
                  id: String(++messageIdCounter.current),
                },
              ]);
              void refreshHistory();
              es.close();
            }
          } catch {
            // 忽略解析错误
          }
        };

        es.onerror = () => {
          // EventSource 在临时断线时会自动携带 Last-Event-ID 重连。
          // 只有浏览器确认连接已关闭时才结束 UI 状态。
          if (es.readyState === EventSource.CLOSED) {
            setIsLoading(false);
            _resetStreaming();
          }
        };
      } catch (err: unknown) {
        setIsLoading(false);
        setAwaitingPlanConfirmation(false);
        _resetStreaming();
        const message = err instanceof Error ? err.message : "提交失败";
        setEvents(prev => [
          ...prev,
          { error: message } as ResearchEvent,
        ]);
        setMessages(prev => [
          ...prev,
          {
            type: "ai",
            content: `提交失败：${message}`,
            id: String(++messageIdCounter.current),
          },
        ]);
      }
    },
    [messages, stop, _resetStreaming, refreshHistory]
  );

  return {
    events,
    messages,
    history,
    activeTaskId,
    plan,
    awaitingPlanConfirmation,
    isLoading,
    isHistoryLoading,
    streamingNode,
    streamingContent,
    submit,
    refreshHistory,
    restoreHistoryItem,
    startNewResearch,
    stop,
  };
}
