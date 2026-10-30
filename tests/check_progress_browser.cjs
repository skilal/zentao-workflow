"use strict";

const assert = require("assert/strict");
const { once } = require("events");
const { createInterface } = require("readline");
const [origin, library, firstId, secondId] = process.argv.slice(2);
const { chromium } = require(library);

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const input = createInterface({ input: process.stdin });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => sessionStorage.setItem("workflow-page", "plan"));
    await page.goto(origin);
    const turn = page.locator(".process-turn").last();
    await turn.getByText("正在核对业务边界", { exact: true }).waitFor();
    assert.equal(await turn.locator('[data-item-id="tool-1"]').getByText("进行中", { exact: true }).count(), 1);
    assert.equal(await turn.locator('[data-item-id="tool-2"]').getByText("进行中", { exact: true }).count(), 1);
    assert.equal(await page.getByText("隐藏思考不应显示", { exact: true }).count(), 0);
    assert.equal(await page.getByText("敏感参数不应显示", { exact: true }).count(), 0);
    await page.reload();
    await turn.getByText("正在核对业务边界", { exact: true }).waitFor();
    await page.locator(".conversation-feed").evaluate(feed => { feed.scrollTop = 0; feed.dispatchEvent(new Event("scroll")); });
    await page.evaluate(() => {
      window.summaryRenders = 0;
      const body = document.querySelector('[data-item-id="summary"] .markdown-body');
      window.summaryObserver = new MutationObserver(() => window.summaryRenders++);
      window.summaryObserver.observe(body, { subtree: true, childList: true, characterData: true });
    });
    const streamed = once(input, "line");
    process.stdout.write("loaded\n");
    assert.equal((await streamed)[0], "streamed");
    await page.waitForFunction(() => document.querySelector('[data-item-id="summary"] .markdown-body').textContent.includes("摘".repeat(1000)));
    assert.equal(await page.locator(".conversation-feed").evaluate(feed => feed.scrollTop), 0);
    assert.ok(await page.evaluate(() => window.summaryRenders) < 30);
    const finished = once(input, "line");
    process.stdout.write("scrolled\n");
    assert.equal((await finished)[0], "finished");
    await page.getByText("最终回复唯一内容", { exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector(".process-turn .ant-collapse-header").getAttribute("aria-expanded") === "false");
    await turn.getByRole("button").click();
    await turn.getByText("已确认业务边界", { exact: true }).waitFor();
    await turn.getByText("可读摘要第二段", { exact: true }).waitFor();
    await turn.getByText("已经读取相关文件，继续处理。", { exact: true }).waitFor();
    assert.equal(await page.getByText("最终回复唯一内容", { exact: true }).count(), 1);
    assert.equal(await turn.getByText("压缩上下文", { exact: true }).count(), 1);
    assert.equal(await turn.getByText("进行中", { exact: true }).count(), 0);
    await page.screenshot({ path: require("path").join(__dirname, "..", "work", "progress-qa.png"), fullPage: true });
    await page.reload();
    await page.getByText("最终回复唯一内容", { exact: true }).waitFor();
    await page.locator(".process-turn").last().getByRole("button").click();
    await page.getByText("已确认业务边界", { exact: true }).waitFor();
    await page.evaluate(id => sessionStorage.setItem("workflow-session", id), secondId);
    await page.reload();
    await page.getByText("其他会话独立内容", { exact: true }).waitFor();
    assert.equal(await page.locator(".process-turn").count(), 0);
    await page.evaluate(id => sessionStorage.setItem("workflow-session", id), firstId);
    await page.reload();
    await page.getByText("最终回复唯一内容", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
  } catch (error) {
    process.stderr.write(await page.locator("body").innerText());
    throw error;
  } finally {
    input.close();
    await browser.close();
  }
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
