// 执行真实绑定页面事件代码；仅 DOM、网络和 FileReader 使用可控替身。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const root = process.env.PAGE_ROOT || path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "pages/fromling/app.js"), "utf8");
const chunk = source.slice(source.indexOf("const bindingFilter ="), source.indexOf("async function renderTools()"));
const OLD = '[{"person_id":"p","platform":"qq","account_id":"old"}]';
const NEW = '[{"person_id":"p","platform":"qq","account_id":"new"}]';

async function setup({ previewPost, confirm, candidates = [] } = {}) {
  const elements = new Map(), readers = [], posts = [];
  const el = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", checked: false, disabled: false, innerHTML: "", textContent: "",
    });
    return elements.get(id);
  };
  const groups = new Map(["approve", "reject"].map((action) => [
    `[data-bind-${action}]`,
    candidates.map((row) => ({ dataset: {
      [action === "approve" ? "bindApprove" : "bindReject"]: row.candidate_id,
    } })),
  ]));
  const context = vm.createContext({
    view: { innerHTML: "" },
    document: { getElementById: el, querySelectorAll: (selector) => groups.get(selector) || [],
      querySelector: () => null },
    get: async (p) => p.endsWith("/overview") ? { platforms: [] } :
      { items: p.endsWith("/candidates") ? structuredClone(candidates) : [] },
    post: async (p, body) => {
      posts.push({ path: p, payload: structuredClone(body) });
      return p.endsWith("/preview") ? (previewPost ? previewPost(body) :
        { rows: 1, mode: body.mode, can_apply: true, preview_token: "synthetic-token" }) : { created: 1 };
    },
    esc: (s) => String(s ?? ""), fmtTime: () => "", askToast: () => {},
    askConfirm: confirm || (async () => true), render: () => {}, settingsCache: null,
    FileReader: class {
      readAsText(file) {
        if (file.failSync) throw new Error("synthetic read failure");
        this.file = file;
        readers.push(this);
      }
    },
  });
  vm.runInContext(chunk, context);
  await context.renderBindings();
  el("bind-operator").value = el("bind-reviewer").value = "human";
  el("bind-reason").value = "synthetic";
  el("bind-content").value = OLD;
  el("bind-mode").value = "replace";
  const click = (id) => el(id).onclick({ target: el(id) });
  return {
    el, readers, posts, groups,
    state: () => vm.runInContext("JSON.parse(JSON.stringify(bindingUpload))", context),
    click,
    prime: async () => {
      await click("bind-upload-preview");
      el("bind-preview-confirmed").checked = true;
    },
    select: (name, content = NEW, extra = {}) => {
      el("bind-file").files = name ? [{ name, content, ...extra }] : [];
      el("bind-file").onchange();
    },
    finish: (reader) => { reader.result = reader.file.content; reader.onload(); },
    writes: () => posts.filter((p) => p.path === "page/bindings/upload"),
  };
}

test("选取文件立即清空预览并禁用提交，不能程序调用绕过 loading", async () => {
  const ui = await setup();
  await ui.prime();
  ui.select("new.json");
  assert.equal(ui.el("bind-preview-confirmed").checked, false);
  for (const id of ["bind-upload-go", "bind-upload-preview", "bind-preview-confirmed"]) {
    assert.equal(ui.el(id).disabled, true);
  }
  await ui.click("bind-upload-preview");
  await ui.click("bind-upload-go");
  assert.equal(ui.posts.length, 1);
  assert.equal(ui.writes().length, 0);
  assert.equal(ui.state().preview, null);
});

for (const failure of ["onerror", "onabort", "sync"]) {
  test(`读取 ${failure} 失败后不准提交，粘贴内容并重新预览后可恢复`, async () => {
    const ui = await setup();
    await ui.prime();
    ui.select("bad.json", NEW, { failSync: failure === "sync" });
    if (failure !== "sync") ui.readers[0][failure]();
    assert.equal(ui.el("bind-upload-go").disabled, true);
    await ui.click("bind-upload-preview");
    await ui.click("bind-upload-go");
    assert.equal(ui.writes().length, 0);
    ui.el("bind-content").value = NEW;
    ui.el("bind-content").oninput();
    assert.equal(ui.el("bind-upload-go").disabled, false);
    await ui.prime();
    await ui.click("bind-upload-go");
    assert.equal(ui.writes()[0].payload.content, NEW);
    assert.equal(ui.writes()[0].payload.filename, "");
  });
}

for (const order of [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]]) {
  test(`三个文件的读取顺序 ${order.join("-")} 只保留最新草稿`, async () => {
    const ui = await setup();
    for (let i = 0; i < 3; i++) ui.select(`${i}.json`, String(i));
    for (const i of order) ui.finish(ui.readers[i]);
    ui.readers[0].onerror();
    ui.readers[1].onabort();
    assert.equal(ui.el("bind-content").value, "2");
    assert.equal(ui.state().filename, "2.json");
    assert.equal(ui.state().fileState, "idle");
  });
}

for (const action of ["cancel", "paste"]) {
  test(`${action} 使在途文件回调失效`, async () => {
    const ui = await setup();
    ui.select("old.json", OLD);
    if (action === "cancel") ui.select(null);
    else {
      ui.el("bind-content").value = NEW;
      ui.el("bind-content").oninput();
    }
    ui.finish(ui.readers[0]);
    assert.equal(ui.el("bind-content").value, action === "cancel" ? "" : NEW);
    assert.equal(ui.state().filename, "");
    assert.equal(ui.state().preview, null);
  });
}

test("同文件名同内容重新选择后旧预览响应也必须作废", async () => {
  let complete;
  const pending = new Promise((resolve) => { complete = resolve; });
  const ui = await setup({ previewPost: () => pending });
  ui.select("same.json", OLD);
  ui.finish(ui.readers[0]);
  const preview = ui.click("bind-upload-preview");
  ui.select("same.json", OLD);
  ui.finish(ui.readers[1]);
  complete({ rows: 1, can_apply: true, preview_token: "old-token" });
  await preview;
  assert.equal(ui.state().preview, null);
  await ui.click("bind-upload-go");
  assert.equal(ui.writes().length, 0);
});

test("确认对话框等待期间选文件，旧上传也不能发送", async () => {
  let confirm;
  const ui = await setup({ confirm: () => new Promise((resolve) => { confirm = resolve; }) });
  await ui.prime();
  const upload = ui.click("bind-upload-go");
  ui.select("new.json");
  confirm(true);
  await upload;
  assert.equal(ui.writes().length, 0);
  assert.equal(ui.el("bind-upload-go").disabled, true);
});

for (const action of ["approve", "reject"]) {
  test(`${action} 携带页面展示时的完整候选快照`, async () => {
    const shown = { candidate_id: "same-id", person_id: "displayed", platform: "qq",
      account_id: "keep", evidence_ref: "shown-ref", confidence: 0.5, updated_at: 1000 };
    const ui = await setup({ candidates: [shown] });
    await ui.groups.get(`[data-bind-${action}]`)[0].onclick();
    assert.equal(ui.posts[0].path, "page/bindings/candidates/review");
    assert.deepEqual(ui.posts[0].payload.expected_candidate, shown);
    assert.equal(ui.posts[0].payload.approve, action === "approve");
  });
}
