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
      await document.fonts.ready;
      const images = [...document.images];
      await Promise.all(images.map(async (image) => {
        if (image.complete) return;
        await new Promise((resolve) => {
          image.addEventListener("load", resolve, { once: true });
          image.addEventListener("error", resolve, { once: true });
        });
      }));
      return {
        imageCount: images.length,
        brokenImages: images
          .filter((image) => !image.complete || image.naturalWidth === 0)
          .map((image) => image.getAttribute("src")),
        tocRows: document.querySelectorAll('a.manual-toc-row[href^="#"]').length,
        fonts: {
          regular: document.fonts.check('400 16px "IBM Plex Sans"'),
          bold: document.fonts.check('700 16px "IBM Plex Sans"'),
          italic: document.fonts.check('italic 400 16px "IBM Plex Sans"'),
          boldItalic: document.fonts.check('italic 700 16px "IBM Plex Sans"'),
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
