#!/usr/bin/env node
/**
 * prepack:打出 wheel 并放进 dist/。
 *
 * 发布包只装"投递壳 + 轮子";Python 代码全在轮子里。版本号必须和 package.json 一致 ——
 * 不一致的包会让用户装到和 npm 版本对不上的平台代码。
 */
const { execFileSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const pkgDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(pkgDir, "..");
const distDir = path.join(pkgDir, "dist");
const version = require("../package.json").version;

fs.rmSync(distDir, { recursive: true, force: true });
fs.mkdirSync(distDir, { recursive: true });

const python = process.env.TESTPLATFORM_BUILD_PYTHON || "python";
execFileSync(python, ["-m", "build", "--wheel", "--outdir", distDir], {
  cwd: repoRoot,
  stdio: "inherit",
});

// build 会在仓库里留下 build/ 与 src/*.egg-info:这些目錄会让 importlib.metadata
// 把平台当成"已安装"(entry point 生效),于是同一轮 pytest 里插件被注册两次 ——
// 打包完就清掉,别把状态污染留给下一次跑测试的人。
for (const leftover of [path.join(repoRoot, "build"), ...fs.readdirSync(path.join(repoRoot, "src"))
  .filter((name) => name.endsWith(".egg-info"))
  .map((name) => path.join(repoRoot, "src", name))]) {
  fs.rmSync(leftover, { recursive: true, force: true });
}

const wheels = fs.readdirSync(distDir).filter((name) => name.endsWith(".whl"));
if (wheels.length !== 1) {
  throw new Error(`prepack:dist/ 里应该正好一个 wheel,实际 ${wheels.length} 个`);
}
if (!wheels[0].includes(version)) {
  throw new Error(`prepack:wheel 文件名里没有版本 ${version}:${wheels[0]}`);
}
const wheelPath = path.join(distDir, wheels[0]);
const sha256 = crypto.createHash("sha256").update(fs.readFileSync(wheelPath)).digest("hex");
// 指纹写在 **dist/ 里**(与 wheel 同生同灭):写进 package.json 会让每次打包都脏工作树,
// 而"仓库里的 package.json 是干净的"本身是可信度的一部分。
fs.writeFileSync(
  path.join(distDir, "wheel.json"),
  `${JSON.stringify({ file: wheels[0], sha256, builtAt: new Date().toISOString() }, null, 2)}
`,
  "utf8",
);
console.log(`prepack:sha256 ${sha256} → dist/wheel.json`);
