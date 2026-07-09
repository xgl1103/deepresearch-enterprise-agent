import { useResearchStream, type ResearchSource } from "@/lib/useResearchStream";
import { useState, useEffect, useRef, useCallback } from "react";
import { ProcessedEvent } from "@/components/ActivityTimeline";
import { WelcomeScreen } from "@/components/WelcomeScreen";
import { ResearchStreamChatView } from "@/components/ResearchStreamChatView";
import { HistorySidebar } from "@/components/HistorySidebar";
import type { ResearchHistoryItem } from "@/lib/api";

export default function App() {
  const [processedEventsTimeline, setProcessedEventsTimeline] = useState<
    ProcessedEvent[]
  >([]);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const processedResearchEventCountRef = useRef(0);
  const researchStream = useResearchStream();

  useEffect(() => {
    if (scrollAreaRef.current) {
      const scrollViewport = scrollAreaRef.current.querySelector(
        "[data-radix-scroll-area-viewport]"
      );
      if (scrollViewport) {
        scrollViewport.scrollTop = scrollViewport.scrollHeight;
      }
    }
  }, [researchStream.messages]);

  // 将异步通道的事件汇入 ActivityTimeline（与 useStream 的 onUpdateEvent 逻辑一致）
  useEffect(() => {
    if (researchStream.events.length < processedResearchEventCountRef.current) {
      processedResearchEventCountRef.current = 0;
    }
    const newEvents = researchStream.events.slice(
      processedResearchEventCountRef.current
    );
    processedResearchEventCountRef.current = researchStream.events.length;

    for (const event of newEvents) {
      let processedEvent: ProcessedEvent | null = null;
      if (event.generate_plan) {
        processedEvent = {
          title: "生成计划",
          data: "研究计划已生成，等待用户确认。",
        };
      } else if (event.generate_query) {
        processedEvent = {
          title: "生成搜索查询",
          data: event.generate_query.search_query?.join(", ") || "",
        };
      } else if (event.web_research) {
        const sources = event.web_research.sources_gathered || [];
        const uniqueLabels = [
          ...new Set(sources.map((source: ResearchSource) => source.label).filter(Boolean)),
        ];
        processedEvent = {
          title: "网络研究",
          data: `已收集 ${sources.length} 条来源。相关主题：${uniqueLabels.slice(0, 3).join(", ") || "暂无标签"}。`,
        };
      } else if (event.reflection) {
        processedEvent = {
          title: "反思和分析",
          data: "正在分析网络研究结果。",
        };
      } else if (event.finalize_answer) {
        processedEvent = {
          title: "最终确定答案",
          data: "正在整理并输出最终报告。",
        };
      }
      if (processedEvent) {
        setProcessedEventsTimeline(prev => [...prev, processedEvent]);
      }
    }
  }, [researchStream.events]);

  const handleSubmit = useCallback(
    (submittedInputValue: string, effort: string, model: string) => {
      console.log('handleSubmit exectued.....', submittedInputValue, effort, model);
      if (!submittedInputValue.trim()) return;
      setProcessedEventsTimeline([]);

      if (researchStream.messages.length === 0) {
        researchStream.submit(submittedInputValue, effort, model);
        return;
      }

      researchStream.submit(submittedInputValue, effort, model, {
        plan: researchStream.plan,
        planStatus: "confirmed",
      });
    },
    [researchStream]
  );

  const handleCancel = useCallback(() => {
    researchStream.stop();
  }, [researchStream]);

  const handleSelectHistory = useCallback(
    (item: ResearchHistoryItem) => {
      setProcessedEventsTimeline([]);
      processedResearchEventCountRef.current = 0;
      researchStream.restoreHistoryItem(item);
    },
    [researchStream]
  );

  const handleNewResearch = useCallback(() => {
    setProcessedEventsTimeline([]);
    processedResearchEventCountRef.current = 0;
    researchStream.startNewResearch();
  }, [researchStream]);

  return (
    <div className="flex h-screen bg-neutral-800 text-neutral-100 font-sans antialiased">
      <HistorySidebar
        items={researchStream.history}
        activeTaskId={researchStream.activeTaskId}
        isLoading={researchStream.isHistoryLoading}
        onSelect={handleSelectHistory}
        onNewResearch={handleNewResearch}
        onRefresh={researchStream.refreshHistory}
      />
      <main className="h-full w-full max-w-4xl mx-auto">
          {researchStream.messages.length === 0 ? (
            <WelcomeScreen
              handleSubmit={handleSubmit}
              isLoading={researchStream.isLoading}
              onCancel={handleCancel}
            />
          ) : (
            <ResearchStreamChatView
              messages={researchStream.messages}
              isLoading={researchStream.isLoading}
              awaitingPlanConfirmation={researchStream.awaitingPlanConfirmation}
              liveActivityEvents={processedEventsTimeline}
              streamingNode={researchStream.streamingNode}
              streamingContent={researchStream.streamingContent}
              onSubmit={handleSubmit}
              onCancel={handleCancel}
              scrollAreaRef={scrollAreaRef}
            />
          )}
      </main>
    </div>
  );
}
