const fs = require("fs");
const http = require("http");
const path = require("path");
const { chromium } = require("playwright");
const packageMetadata = require("playwright/package.json");


const [documentArgument, outputArgument] = process.argv.slice(2);
if (!documentArgument || !outputArgument) {
  throw new Error("Usage: node render_pdf.js <document.html> <output.pdf>");
}

const documentPath = path.resolve(documentArgument);
const outputPath = path.resolve(outputArgument);
const stateRoot = path.resolve(path.dirname(documentPath), "..");

const mimeTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".ttf": "font/ttf",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};


function startStaticServer() {
  const server = http.createServer((request, response) => {
    const requestUrl = new URL(request.url, "http://127.0.0.1");
    const relativePath = decodeURIComponent(requestUrl.pathname).replace(/^\/+/, "");
    const resolvedPath = path.resolve(stateRoot, relativePath);
    const relativeToRoot = path.relative(stateRoot, resolvedPath);
    if (relativeToRoot.startsWith("..") || path.isAbsolute(relativeToRoot)) {
      response.writeHead(403);
      response.end("Forbidden");
      return;
    }
    fs.readFile(resolvedPath, (error, data) => {
      if (error) {
        response.writeHead(error.code === "ENOENT" ? 404 : 500);
        response.end(error.message);
        return;
      }
      const mimeType = mimeTypes[path.extname(resolvedPath).toLowerCase()] || "application/octet-stream";
      response.writeHead(200, { "Content-Type": mimeType, "Cache-Control": "no-store" });
      response.end(data);
    });
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}


async function main() {
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  const server = await startStaticServer();
  let browser;
  const browserErrors = [];
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(message.text());
    });
    page.on("pageerror", (error) => browserErrors.push(error.message));
    const address = server.address();
    const relativeDocument = path.relative(stateRoot, documentPath).split(path.sep).join("/");
    const url = `http://127.0.0.1:${address.port}/${relativeDocument}`;
    await page.goto(url, { waitUntil: "load", timeout: 120000 });
    await page.emulateMedia({ media: "print" });
    const resources = await page.evaluate(async () => {
      const waitAtMost = async (promise, milliseconds) => {
        let timer;
        try {
          return await Promise.race([
            promise,
            new Promise((resolve) => {
              timer = setTimeout(resolve, milliseconds);
            }),
          ]);
        } finally {
          clearTimeout(timer);
        }
      };
      const requiredFonts = [
        '400 16px "IBM Plex Sans"',
        '700 16px "IBM Plex Sans"',
        'italic 400 16px "IBM Plex Sans"',
        'italic 700 16px "IBM Plex Sans"',
      ];
      const images = [...document.images];
      for (const image of images) {
        image.loading = "eager";
        image.removeAttribute("loading");
      }
      const imagePromises = images.map(async (image) => {
        if (image.complete) return;
        await new Promise((resolve) => {
          let settled = false;
          const finish = () => {
            if (settled) return;
            settled = true;
            resolve();
          };
          image.addEventListener("load", finish, { once: true });
          image.addEventListener("error", finish, { once: true });
          // A local image can finish between the first `complete` check and
          // listener registration. Re-check after installing the listeners so
          // that fast cache hits cannot leave PDF rendering waiting forever.
          if (image.complete) finish();
        });
      });
      await waitAtMost(
        Promise.all([
          Promise.all(
            requiredFonts.map((font) => document.fonts.load(font, "Русский текст")),
          ),
          document.fonts.ready,
          Promise.all(imagePromises),
        ]),
        30000,
      );
      return {
        imageCount: images.length,
        brokenImages: images
          .filter((image) => !image.complete || image.naturalWidth === 0)
          .map((image) => image.getAttribute("src")),
        tocRows: document.querySelectorAll('a.manual-toc-row[href^="#"]').length,
        fonts: {
          regular: document.fonts.check(requiredFonts[0], "Русский текст"),
          bold: document.fonts.check(requiredFonts[1], "Русский текст"),
          italic: document.fonts.check(requiredFonts[2], "Русский текст"),
          boldItalic: document.fonts.check(requiredFonts[3], "Русский текст"),
        },
      };
    });
    if (resources.brokenImages.length) {
      throw new Error(`Images failed to load: ${resources.brokenImages.slice(0, 8).join(", ")}`);
    }
    if (!Object.values(resources.fonts).every(Boolean)) {
      throw new Error(`Required IBM Plex Sans faces failed to load: ${JSON.stringify(resources.fonts)}`);
    }
    if (browserErrors.length) {
      throw new Error(`Browser errors during PDF generation: ${browserErrors.slice(0, 8).join("; ")}`);
    }
    await page.pdf({
      path: outputPath,
      printBackground: true,
      preferCSSPageSize: true,
      format: "A4",
      margin: { top: "0", right: "0", bottom: "0", left: "0" },
      tagged: true,
      outline: true,
    });
    process.stdout.write(JSON.stringify({
      ...resources,
      browserErrors,
      browserVersion: browser.version(),
      playwrightVersion: packageMetadata.version,
    }));
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}


main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
