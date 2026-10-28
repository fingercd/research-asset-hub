import loadingHtml from "../../public/loading.html?raw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const steps = ["预检环境", "启动数据库", "启动搜索引擎", "数据迁移", "启动应用服务"];
type Progress = { step: string; status: "pending" | "done" | "failed"; detail?: string };
type LoadingWindow = Window & { __hubProgress?: (payload: Progress) => void };

function emit(payload: Progress) {
  (window as LoadingWindow).__hubProgress!(payload);
}
function row(step: string) {
  return document.querySelector<HTMLElement>(`li[data-step="${step}"]`)!;
}

// Execute the shipped inline loading script in JSDOM: no HTTP server or browser fixture.
beforeEach(() => {
  const parsed = new DOMParser().parseFromString(loadingHtml, "text/html");
  document.body.innerHTML = parsed.body.innerHTML;
  const script = parsed.querySelector("script")!.textContent!;
  new Function("window", "document", script)(window, document);
});
afterEach(() => {
  delete (window as LoadingWindow).__hubProgress;
  document.body.innerHTML = "";
});

describe("native loading page progress contract", () => {
  it("renders the five real phases and advances only on completion", () => {
    expect(document.querySelectorAll("#steps li")).toHaveLength(5);
    expect(row(steps[0]!).dataset.state).toBe("pending");
    expect(document.getElementById("progress")).toHaveAttribute("aria-valuenow", "0");
    steps.forEach((step, index) => {
      emit({ step, status: "pending" });
      expect(row(step).querySelector(".step-state")).toHaveTextContent("进行中");
      expect(document.getElementById("progress")).toHaveAttribute("aria-valuenow", String(index * 20));
      emit({ step, status: "done" });
      expect(row(step).dataset.state).toBe("done");
      expect(document.getElementById("progress")).toHaveAttribute("aria-valuenow", String((index + 1) * 20));
    });
    expect(document.getElementById("progress-count")).toHaveTextContent("已完成 5 / 5 项");
    expect(document.getElementById("bar")!.style.width).toBe("100%");
    expect(document.getElementById("summary")).toHaveTextContent("所有启动阶段已完成");
    expect(document.getElementById("error")).toHaveAttribute("hidden");
  });

  it("keeps readable failure details and clears previous progress on retry", () => {
    emit({ step: steps[0]!, status: "done" });
    emit({ step: steps[1]!, status: "pending" });
    const detail = "端口 5432 已被占用\n请检查数据库进程。";
    emit({ step: steps[1]!, status: "failed", detail });
    expect(row(steps[1]!).dataset.state).toBe("failed");
    expect(document.getElementById("error-detail")!.textContent).toBe(detail);
    expect(document.getElementById("error")).not.toHaveAttribute("hidden");
    expect(document.getElementById("progress")).toHaveAttribute("aria-valuenow", "20");
    emit({ step: steps[0]!, status: "pending" });
    expect(document.getElementById("error")).toHaveAttribute("hidden");
    expect(document.getElementById("error-detail")).toBeEmptyDOMElement();
    expect(row(steps[0]!).dataset.state).toBe("pending");
    expect(document.querySelectorAll("#steps li[data-state='waiting']")).toHaveLength(4);
    expect(document.getElementById("progress")).toHaveAttribute("aria-valuenow", "0");
  });
});
