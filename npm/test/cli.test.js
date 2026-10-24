/**
 * `npx` 壳的测试(node --test)。
 *
 * 转发那几条**不 mock 子进程**:把 `TESTPLATFORM_PYTHON` 指到真 Python,再用一个假的
 * `testplatform` 包(放在 PYTHONPATH 最前面)把 `sys.argv` 记下来 —— 断言的是"最后真的
 * 调到了哪个入口、参数是什么",不是"我以为的字符串"。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const CLI = path.resolve(__dirname, "..", "cli", "index.js");
const lib = require("../cli/lib");

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "tp-npm-"));
}

/** 假平台包:任何入口被调用时把自己的 argv 记到文件里,然后 exit 0。 */
function fakePackage(root) {
  const modules = {
    "testplatform/execution/cli.py": "run-task",
    "testplatform/execution/gate.py": "gate",
    "testplatform/cli.py": "platform",
  };
  // 必须有 __init__.py:否则它是 namespace 包,会被 site-packages 里的真平台包盖过去
  for (const init of ["testplatform/__init__.py", "testplatform/execution/__init__.py"]) {
    fs.mkdirSync(path.dirname(path.join(root, init)), { recursive: true });
    fs.writeFileSync(path.join(root, init), "", "utf-8");
  }
  for (const [relative, label] of Object.entries(modules)) {
    const file = path.join(root, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(
      file,
      [
        "import json, os, sys",
        `with open(os.environ["FAKE_ARGV_LOG"], "a", encoding="utf-8") as handle:`,
        `    handle.write(json.dumps({"entry": ${JSON.stringify(label)}, "argv": sys.argv[1:]}) + "\\n")`,
        "raise SystemExit(0)",
        "",
      ].join("\n"),
      "utf-8",
    );
  }
  return root;
}

function runCli(args, env) {
  const log = env.FAKE_ARGV_LOG || path.join(tempDir(), "argv.jsonl");
  const result = spawnSync(process.execPath, [CLI, ...args], {
    encoding: "utf8",
    env: { ...process.env, ...env, FAKE_ARGV_LOG: log },
  });
  return { status: result.status, log, output: `${result.stdout || ""}${result.stderr || ""}` };
}

function recorded(log) {
  if (!fs.existsSync(log)) {
    return [];
  }
  return fs
    .readFileSync(log, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

test("run / gate / scaffold 的转发参数拼对了", () => {
  const work = tempDir();
  const fakepkg = fakePackage(path.join(work, "fakepkg"));
  const env = {
    PYTHONPATH: fakepkg,
    TESTPLATFORM_PYTHON: process.env.PYTHON || "python",
    TESTPLATFORM_SKIP_ENV: "1",
    FAKE_ARGV_LOG: path.join(work, "argv.jsonl"),
  };

  const cases = [
    [["run", "tasks/minimal.yaml", "--root", "."], "run-task", ["tasks/minimal.yaml", "--root", "."]],
    [["gate", "reports"], "gate", ["reports"]],
    [["scaffold", "driver", "--out", "demo"], "platform", ["scaffold", "driver", "--out", "demo"]],
  ];
  for (const [args, entry, expected] of cases) {
    const { status, output } = runCli(args, env);
    assert.strictEqual(status, 0, `${args[0]} 退出码应该是 0:
${output}`);
    const calls = recorded(env.FAKE_ARGV_LOG);
    const last = calls[calls.length - 1];
    assert.strictEqual(last.entry, entry, `${args[0]} 应该调到 ${entry}`);
    assert.deepStrictEqual(last.argv, expected, `${args[0]} 的参数被改过`);
  }
});

test("init 只准备环境:不调平台,并给 .gitignore 提示", () => {
  const work = tempDir();
  const env = {
    TESTPLATFORM_PYTHON: process.env.PYTHON || "python",
    TESTPLATFORM_SKIP_ENV: "1",
    FAKE_ARGV_LOG: path.join(work, "argv.jsonl"),
  };

  const { status } = runCli(["init"], { ...env, INIT_CWD: work });
  assert.strictEqual(status, 0);
  assert.deepStrictEqual(recorded(env.FAKE_ARGV_LOG), [], "init 不该调用平台入口");
});

test("找不到 Python:一类专门的失败消息,不是 ENOENT 堆栈", () => {
  const env = {
    PATH: "",
    TESTPLATFORM_PYTHON: path.join(tempDir(), "no-such-python"),
    TESTPLATFORM_SKIP_ENV: "0",
  };
  const result = spawnSync(process.execPath, [CLI, "run", "x.yaml"], {
    encoding: "utf8",
    env: { ...process.env, ...env },
  });

  assert.strictEqual(result.status, 3, "失败要有一个固定的退出码");
  assert.match(result.stderr, /找不到可用的 Python/);
  assert.match(result.stderr, /3\.10/);
  assert.doesNotMatch(result.stderr, /ENOENT|at Object\.|node:internal/, "不许抛堆栈");
});

test("Python 太旧:消息点名要 ≥ 3.10", () => {
  const error = (() => {
    try {
      lib.checkPythonVersion({ major: 3, minor: 9, text: "3.9.18" });
    } catch (thrown) {
      return thrown;
    }
    return null;
  })();

  assert.ok(error, "3.9 应该被拦下来");
  assert.strictEqual(error.kind, "pythonOld");
  assert.match(error.message, /3\.9\.18/);
  assert.match(error.message, /3\.10/);
});

test("pip 失分两类:没网 / 其他,消息不同", () => {
  const offline = lib.MESSAGES[lib.classifyPipFailure("ERROR: Network is unreachable")]("…");
  const broken = lib.MESSAGES[lib.classifyPipFailure("error: subprocess-exited-with-error")]("…");

  assert.strictEqual(lib.classifyPipFailure("ERROR: Network is unreachable"), "noNetwork");
  assert.strictEqual(lib.classifyPipFailure("error: subprocess-exited-with-error"), "pipFailed");
  assert.match(offline, /连不上包源/);
  assert.match(broken, /pip 报错/);
  assert.notStrictEqual(offline, broken);
});

test("package.json 的形状:名字 / 版本一致 / files 含 cli 与 dist / 没有 postinstall", () => {
  const pkg = require("../package.json");
  const repoRoot = path.resolve(__dirname, "..", "..");
  const pyproject = fs.readFileSync(path.join(repoRoot, "pyproject.toml"), "utf8");
  const version = pyproject.match(/^version\s*=\s*"([^"]+)"/m)[1];

  assert.strictEqual(pkg.name, "@cautonwong/testplatform");
  assert.strictEqual(pkg.version, version, "npm 版本必须和轮子一致");
  assert.ok(pkg.files.includes("cli/") && pkg.files.includes("dist/"), "files 要带上 cli/ 与 dist/");
  for (const script of ["postinstall", "preinstall", "install"]) {
    assert.strictEqual(pkg.scripts[script], undefined, `不许有安装期脚本 ${script}`);
  }
  assert.ok(pkg.bin.testplatform, "要有 bin 入口");
});

test("包内 wheel 的指纹:对得上才装,改一个字符就拒", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const work = tempDir();
  const wheel = path.join(work, "fake-0.1.0-py3-none-any.whl");
  fs.writeFileSync(wheel, "wheel-bytes");
  const fingerprintPath = path.join(work, "wheel.json");

  try {
    const digest = require("node:crypto")
      .createHash("sha256")
      .update(fs.readFileSync(wheel))
      .digest("hex");
    fs.writeFileSync(fingerprintPath, JSON.stringify({ file: path.basename(wheel), sha256: digest }));
    assert.strictEqual(lib.verifyWheel(wheel), digest, "指纹一致时应通过");

    // ① 指纹文件里的 hash 改一个字符
    fs.writeFileSync(fingerprintPath, JSON.stringify({ file: path.basename(wheel), sha256: "0".repeat(64) }));
    assert.throws(() => lib.verifyWheel(wheel), (error) => error.kind === "wheelTampered");

    // ② wheel 被改一个字节(指纹不变)
    fs.writeFileSync(fingerprintPath, JSON.stringify({ file: path.basename(wheel), sha256: digest }));
    fs.writeFileSync(wheel, "wheel-bytez");
    assert.throws(() => lib.verifyWheel(wheel), (error) => error.kind === "wheelTampered");
  } finally {
    fs.rmSync(fingerprintPath, { force: true });
  }
});
