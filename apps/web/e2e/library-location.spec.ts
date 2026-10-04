import { expect, test, type Page } from "@playwright/test";

async function mockSettings(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/health")) {
      await route.fulfill({ json: { status: "healthy", components: {} } });
      return;
    }
    if (path.endsWith("/settings")) {
      await route.fulfill({ json: { app: { mode: "local" } } });
      return;
    }
    if (path.endsWith("/mcp-config")) {
      await route.fulfill({
        json: {
          server_name: "research-assets",
          config: { mcpServers: {} },
          tools: [],
          read_only: true,
          data_dir: "D:/ConfiguredLibrary",
          bundled_runtime: true,
          module_available: true,
        },
      });
      return;
    }
    await route.fulfill({ json: { items: [], next_cursor: null } });
  });
}
async function mockNative(page: Page, locked = false) {
  await page.addInitScript(
    ({ locked }) => {
      const commands: Array<{ command: string; args: unknown }> = [];
      Object.assign(window, {
        isTauri: true,
        __libraryCommands: commands,
        __TAURI_INTERNALS__: {
          invoke: async (command: string, args: unknown) => {
            commands.push({ command, args });
            if (command === "get_data_directory")
              return {
                path: "D:/CurrentLibrary",
                locked_by_environment: locked,
              };
            if (command === "choose_data_directory")
              return { path: "E:/NewLibrary", is_existing_library: false };
            if (command === "switch_data_directory") return;
            throw new Error(`Unexpected native command: ${command}`);
          },
        },
      });
    },
    { locked },
  );
}

test("桌面设置先选目录，再显式切换并重启", async ({ page }) => {
  await mockSettings(page);
  await mockNative(page);
  await page.goto("/settings");
  const section = page.getByRole("region", { name: "资料库位置" });
  await expect(
    section.getByText("D:/CurrentLibrary", { exact: true }),
  ).toBeVisible();
  await section.getByRole("button", { name: "选择资料库目录" }).click();
  await expect(
    section.getByText("将在此目录创建空资料库。", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      Reflect.get(window, "__libraryCommands").filter(
        (call: { command: string }) => call.command === "switch_data_directory",
      ),
    ),
  ).toEqual([]);
  await section
    .getByRole("button", { name: "切换并重启", exact: true })
    .click();
  await expect(
    section.getByText("已提交切换，正在重启应用…", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      Reflect.get(window, "__libraryCommands").find(
        (call: { command: string }) => call.command === "switch_data_directory",
      ),
    ),
  ).toEqual({
    command: "switch_data_directory",
    args: { path: "E:/NewLibrary" },
  });
  await expect(
    section.getByText("D:/CurrentLibrary", { exact: true }),
  ).toBeVisible();
});

test("启动环境锁定资料库时禁用目录选择", async ({ page }) => {
  await mockSettings(page);
  await mockNative(page, true);
  await page.goto("/settings");
  const section = page.getByRole("region", { name: "资料库位置" });
  await expect(
    section.getByRole("button", { name: "选择资料库目录" }),
  ).toBeDisabled();
  await expect(
    section.getByText(/当前位置由启动配置 HUB_DATA_DIR 固定/),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      Reflect.get(window, "__libraryCommands").map(
        (call: { command: string }) => call.command,
      ),
    ),
  ).toEqual(["get_data_directory"]);
});

test("浏览器设置展示实际配置目录，不提供原生选择和切换按钮", async ({
  page,
}) => {
  await mockSettings(page);
  await page.goto("/settings");
  const section = page.getByRole("region", { name: "资料库位置" });
  await expect(
    section.getByText("D:/ConfiguredLibrary", { exact: true }),
  ).toBeVisible();
  await expect(
    section.getByText(/浏览器版的资料库目录由服务启动配置确定/),
  ).toBeVisible();
  await expect(
    section.getByRole("button", { name: "选择资料库目录" }),
  ).toHaveCount(0);
  await expect(
    section.getByRole("button", { name: "切换并重启", exact: true }),
  ).toHaveCount(0);
});
