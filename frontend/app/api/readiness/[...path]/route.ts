import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const origin = request.headers.get("origin");
  // Railway terminates HTTPS before forwarding to Next's internal HTTP URL.
  // Validate against the public host rather than that internal origin.
  const publicHost = process.env.RAILWAY_PUBLIC_DOMAIN || request.headers.get("x-forwarded-host")?.split(",")[0].trim() || request.headers.get("host");
  if (request.method !== "GET" && origin && new URL(origin).host !== publicHost) {
    return NextResponse.json({ detail: "This request must come from the app." }, { status: 403 });
  }
  const { path } = await context.params;
  const base = process.env.NEXT_PUBLIC_API_URL;
  if (!base) return NextResponse.json({ detail: "The backend URL is missing." }, { status: 503 });
  const headers = new Headers();
  // Keep the backend service key on the server. Operators use the app's
  // existing access flow instead of a second readiness-specific login.
  const serviceKey = process.env.READINESS_OPERATOR_KEY;
  if (serviceKey) headers.set("x-readiness-key", serviceKey);
  for (const name of ["cookie", "content-type"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  try {
    const response = await fetch(`${base.replace(/\/$/, "")}/api/v1/tenant-readiness/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`, {
      method: request.method, headers, cache: "no-store", redirect: "error",
      body: request.method === "GET" ? undefined : await request.arrayBuffer(),
      signal: AbortSignal.timeout(25000),
    });
    const outgoing = new Headers({ "Cache-Control": "no-store" });
    for (const name of ["content-type", "content-disposition", "set-cookie"]) {
      const value = response.headers.get(name);
      if (value) outgoing.set(name, value);
    }
    return new NextResponse(await response.arrayBuffer(), { status: response.status, headers: outgoing });
  } catch {
    return NextResponse.json({ detail: "The server could not be reached. Your queued checks are saved; try again shortly." }, { status: 502 });
  }
}

export const GET = proxy;
export const POST = proxy;
