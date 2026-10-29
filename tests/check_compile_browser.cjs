"use strict";

const path = require("path");
const assert = require("assert/strict");
const [origin, modules, screenshot] = process.argv.slice(2);
const { chromium } = require(path.join(modules, "playwright"));

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const page = await browser.newPage({ viewport: { width: 1500, height: 1050 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => { if (!sessionStorage.getItem("workflow-page")) sessionStorage.setItem("workflow-page", "plan"); });
    await page.goto(origin);
    await page.getByRole("button", { name: "允许下载并继续", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "跳过该仓库本轮检查", exact: true }).count(), 1);
    assert.equal(await page.getByRole("radio", { checked: true }).count(), 0);
    await page.reload();
    await page.getByRole("button", { name: "允许下载并继续", exact: true }).waitFor();
    await page.screenshot({ path: screenshot, fullPage: true });
    await page.getByRole("button", { name: "允许下载并继续", exact: true }).click();
    await page.getByText("编译检查通过。", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "允许下载并继续", exact: true }).count(), 0);
    // 通过现有会话列表选择，避免依赖页面内部模型字段。
    await page.getByText("开始前勾选编译检查", { exact: false }).first().click();
    await page.getByRole("menuitem", { name: "工作项与资料", exact: false }).click();
    const sessionSwitch = page.getByRole("checkbox", { name: "Review 前自动检查后端编译", exact: true });
    await sessionSwitch.waitFor();
    assert.equal(await sessionSwitch.isChecked(), false);
    await sessionSwitch.check();
    await page.reload();
    await sessionSwitch.waitFor();
    assert.equal(await sessionSwitch.isChecked(), true);
    await page.getByRole("menuitem", { name: "工作流设置", exact: false }).click();
    const globalSwitch = page.getByRole("checkbox", { name: "Review 前自动检查后端编译", exact: true });
    await globalSwitch.waitFor();
    assert.equal(await globalSwitch.isChecked(), false);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
