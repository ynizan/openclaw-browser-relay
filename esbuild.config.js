import * as esbuild from 'esbuild';
import { cpSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const isWatch = process.argv.includes('--watch');

const buildOptions = {
  entryPoints: [
    resolve(__dirname, 'src/background.ts'),
    resolve(__dirname, 'src/options.ts'),
  ],
  bundle: true,
  outdir: resolve(__dirname, 'dist'),
  format: 'esm',
  target: 'chrome120',
  minify: !isWatch,
  sourcemap: isWatch ? 'inline' : false,
  logLevel: 'info',
};

async function build() {
  mkdirSync(resolve(__dirname, 'dist'), { recursive: true });

  cpSync(resolve(__dirname, 'src/manifest.json'), resolve(__dirname, 'dist/manifest.json'));
  cpSync(resolve(__dirname, 'src/options.html'), resolve(__dirname, 'dist/options.html'));
  cpSync(resolve(__dirname, 'src/icons'), resolve(__dirname, 'dist/icons'), { recursive: true });

  if (isWatch) {
    const ctx = await esbuild.context(buildOptions);
    await ctx.watch();
    console.log('Watching for changes...');
  } else {
    await esbuild.build(buildOptions);
  }
}

build().catch((err) => {
  console.error(err);
  process.exit(1);
});
