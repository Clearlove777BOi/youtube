export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    // 将请求转发到您的Python应用
    return await fetch(`https://your-app-name.pages.dev${url.pathname}${url.search}`);
  }
};