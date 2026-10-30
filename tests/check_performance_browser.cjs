"use strict";

const assert = require("assert/strict");
const { once } = require("events");
const { createInterface } = require("readline");
const [origin, library] = process.argv.slice(2);
const { chromium } = require(library);

(async () => {
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const input = createInterface({ input: process.stdin });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(() => sessionStorage.setItem("workflow-page", "plan"));
    await page.goto(origin);
    const submit = page.getByRole("button", { name: "提交回答并继续", exact: true });
    const choice = page.getByRole("radio", { name: "纳入", exact: true });
    await page.getByText("是否将文件当前内容纳入？", { exact: true }).last().waitFor();
    assert.equal(await submit.isDisabled(), true);
    assert.equal(await choice.isChecked(), false);
    const stopped = once(input, "line");
    process.stdout.write("waiting\n");
    assert.equal((await stopped)[0], "stopped");
    await page.reload();
    await submit.waitFor();
    assert.equal(await submit.isEnabled(), true);
    assert.equal(await choice.isChecked(), false);
    await submit.click();
    await page.getByText("尚未回答完整", { exact: true }).waitFor();
    await choice.check();
    await submit.click();
    await page.getByText("续聊处理中", { exact: true }).first().waitFor();
    assert.equal(await page.locator(".question").count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    input.close();
    await browser.close();
  }
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
