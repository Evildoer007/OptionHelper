import { promises as fs } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import {
  cliRender,
  createConfig,
  validateConfig,
} from './core.mjs';

function usage() {
  return `用法：
  node cli.mjs new --product <分类编号或名称> [--output <json路径>]
  node cli.mjs validate --input <json路径>
  node cli.mjs inspect --input <json路径>
  node cli.mjs render --input <json路径> --output <svg路径>

说明：命令行只能生成JSON或临时SVG；正式SVG只能在网页中人工确认发布。`;
}

function args(argv) {
  const [command, ...rest] = argv;
  const values = {};
  for (let index = 0; index < rest.length; index += 1) {
    if (rest[index].startsWith('--')) values[rest[index].slice(2)] = rest[index + 1];
  }
  return { command, values };
}

function emit(body, code = 0) {
  process.stdout.write(`${JSON.stringify(body, null, 2)}\n`);
  process.exitCode = code;
}

async function readConfig(file) {
  return JSON.parse(await fs.readFile(path.resolve(file), 'utf8'));
}

const { command, values } = args(process.argv.slice(2));
try {
  if (!command || command === '--help' || command === 'help') {
    process.stdout.write(`${usage()}\n`);
  } else if (command === 'new') {
    if (!values.product) throw new Error('new需要--product。');
    const config = await createConfig(values.product);
    if (values.output) {
      const output = path.resolve(values.output);
      await fs.mkdir(path.dirname(output), { recursive: true });
      await fs.writeFile(output, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
    }
    emit({ ok: true, config, output: values.output ? path.resolve(values.output) : 'stdout' });
  } else if (command === 'validate' || command === 'inspect' || command === 'render') {
    if (!values.input) throw new Error(`${command}需要--input。`);
    const config = await readConfig(values.input);
    const validation = await validateConfig(config, { requireComplete: false });
    if (command === 'render' && validation.errors.length === 0) {
      if (!values.output) throw new Error('render需要--output；正式SVG只能在网页中人工确认发布。');
      const output = path.resolve(values.output);
      const result = await cliRender(config, output);
      emit({ ok: result.errors.length === 0, validation: result, output }, result.errors.length ? 1 : 0);
    } else if (command === 'inspect') {
      emit({ ok: validation.errors.length === 0, product: config.library?.name, scenarioCount: config.library?.scenarios?.length || 0, validation }, validation.errors.length ? 1 : 0);
    } else {
      emit({ ok: validation.errors.length === 0, validation }, validation.errors.length ? 1 : 0);
    }
  } else {
    throw new Error(`未知命令：${command}`);
  }
} catch (error) {
  emit({ ok: false, error: error instanceof Error ? error.message : '命令执行失败。' }, 1);
}
