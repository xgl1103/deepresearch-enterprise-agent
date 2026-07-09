import { FileText, Loader2, Plus, RefreshCw } from "lucide-react";
import type { ResearchHistoryItem } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

interface HistorySidebarProps {
  items: ResearchHistoryItem[];
  activeTaskId: string;
  isLoading: boolean;
  error: string;
  onSelect: (item: ResearchHistoryItem) => void;
  onNewResearch: () => void;
  onRefresh: () => void;
}

function formatTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusText(status: string): string {
  const map: Record<string, string> = {
    completed: "已完成",
    paused: "待确认",
    running: "运行中",
    queued: "排队中",
    failed: "失败",
    cancelled: "已取消",
  };
  return map[status] || status || "未知";
}

export function HistorySidebar({
  items,
  activeTaskId,
  isLoading,
  error,
  onSelect,
  onNewResearch,
  onRefresh,
}: HistorySidebarProps) {
  return (
    <aside className="hidden md:flex h-full w-72 shrink-0 flex-col border-r border-neutral-700 bg-neutral-900/70">
      <div className="p-3 border-b border-neutral-700 space-y-2">
        <Button
          className="w-full justify-start bg-neutral-700 hover:bg-neutral-600 text-neutral-100"
          onClick={onNewResearch}
        >
          <Plus className="h-4 w-4" />
          新建研究
        </Button>
        <div className="flex items-center justify-between px-1">
          <span className="text-xs text-neutral-400">历史记录</span>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-100"
            onClick={onRefresh}
            disabled={isLoading}
            title="刷新历史记录"
          >
            {isLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="h-4 w-4" />
            )}
          </Button>
        </div>
      </div>

      <ScrollArea className="flex-1">
        <div className="p-2 space-y-1">
          {error ? (
            <div className="px-3 py-6 text-sm text-red-300">
              历史记录加载失败：{error}
            </div>
          ) : items.length === 0 && !isLoading ? (
            <div className="px-3 py-6 text-sm text-neutral-500">暂无历史记录</div>
          ) : (
            items.map((item) => {
              const active = item.task_id === activeTaskId;
              return (
                <button
                  key={item.task_id}
                  type="button"
                  onClick={() => onSelect(item)}
                  className={cn(
                    "w-full rounded-lg px-3 py-2 text-left transition-colors",
                    "hover:bg-neutral-800",
                    active && "bg-neutral-800 ring-1 ring-neutral-700"
                  )}
                >
                  <div className="flex items-start gap-2">
                    <FileText className="mt-0.5 h-4 w-4 shrink-0 text-neutral-500" />
                    <div className="min-w-0 flex-1">
                      <div className="line-clamp-2 text-sm leading-5 text-neutral-100">
                        {item.title || "未命名研究"}
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-2 text-xs text-neutral-500">
                        <span>{statusText(item.status)}</span>
                        <span>{formatTime(item.updated_at || item.created_at)}</span>
                      </div>
                    </div>
                  </div>
                </button>
              );
            })
          )}
        </div>
      </ScrollArea>
    </aside>
  );
}
