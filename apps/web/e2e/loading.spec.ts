import { expect, test, type Page } from "@playwright/test";

const steps = ["预检环境", "启动数据库", "启动搜索引擎", "数据迁移", "启动应用服务"];
type Progress = { step: string; status: "pending" | "done" | "failed"; detail?: string };

async function emit(page: Page, payload: Progress) {
  await page.evaluate((event) => {
    (window as Window & { __hubProgress: (payload: Progress) => void }).__hubProgress(event);
  }, payload);
}

function row(page: Page, step: string) {
  return page.locator("#steps li").filter({ has: page.locator(".step-name", { hasText: step }) });
}

test.beforeEach(async ({ page }) => {
  await page.goto("/loading.html");
});

test("启动页显示真实五阶段，并仅按完成事件更新进度", async ({ page }) => {
  await expect(page.locator("#steps li")).toHaveCount(5);
  await expect(row(page, steps[0])).toHaveAttribute("data-state", "pending");
  await expect(page.getByRole("progressbar", { name: "启动进度" })).toHaveAttribute("aria-valuenow", "0");

  for (let index = 0; index < steps.length; index++) {
    const step = steps[index];
    await expect(row(page, step).locator(".step-name")).toHaveText(step);
    await emit(page, { step, status: "pending" });
    await expect(row(page, step)).toHaveAttribute("data-state", "pending");
    await expect(row(page, step).locator(".step-state")).toHaveText("进行中");
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", String(index * 20));

    await emit(page, { step, status: "done" });
    await expect(row(page, step)).toHaveAttribute("data-state", "done");
    await expect(row(page, step).locator(".step-state")).toHaveText("已完成");
    await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", String((index + 1) * 20));
  }

  await expect(page.locator("#progress-count")).toHaveText("已完成 5 / 5 项");
  expect(await page.locator("#bar").evaluate((element: HTMLElement) => element.style.width)).toBe("100%");
  await expect(page.locator("#summary")).toContainText("所有启动阶段已完成");
  await expect(page.locator("#error")).toBeHidden();
});

test("失败保留可读详情，托盘重试开始时清空旧错误和进度", async ({ page }) => {
  await emit(page, { step: steps[0], status: "done" });
  await emit(page, { step: steps[1], status: "pending" });
  const detail = "端口 5432 已被占用\n请检查数据库进程。";
  await emit(page, { step: steps[1], status: "failed", detail });

  await expect(row(page, steps[1])).toHaveAttribute("data-state", "failed");
  await expect(row(page, steps[1]).locator(".step-state")).toHaveText("失败");
  await expect(page.locator("#error-detail")).toHaveText(detail);
  await expect(page.locator("#error")).toContainText("系统托盘菜单选择“重试启动”");
  await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "20");

  await emit(page, { step: steps[0], status: "pending" });
  await expect(page.locator("#error")).toBeHidden();
  await expect(page.locator("#error-detail")).toBeEmpty();
  await expect(row(page, steps[0])).toHaveAttribute("data-state", "pending");
  await expect(row(page, steps[1])).toHaveAttribute("data-state", "waiting");
  await expect(page.locator("#steps li[data-state='waiting']")).toHaveCount(4);
  await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
  await expect(page.locator("#progress-count")).toHaveText("已完成 0 / 5 项");
});
