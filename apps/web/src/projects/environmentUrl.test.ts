/**
 * 环境地址的前端校验：与后端同一套规则，用来在输入框旁就地报错。
 *
 * 这里锁的是**接受／拒绝的边界**，而不是文案措辞：边界一旦与后端分叉，用户就会遇到
 * “前端放行、后端打回”这种说不清谁对的情况。
 */
import { describe, expect, it } from "vitest";

import {
  ENVIRONMENT_URL_EXAMPLE,
  ENVIRONMENT_URL_IP_EXAMPLE,
  validateEnvironmentUrl,
} from "./environmentUrl";

/** 保留占位：RFC 2606 的保留域名，或 RFC 5737 的文档用网段。 */
const RESERVED_TLDS = ["example", "test", "invalid", "localhost"];
const DOCUMENTATION_IPV4 = ["192.0.2.", "198.51.100.", "203.0.113."];

function isReservedPlaceholder(url: string): boolean {
  const host = new URL(url).hostname;
  if (/^[0-9.]+$/.test(host)) {
    return DOCUMENTATION_IPV4.some((prefix) => host.startsWith(prefix));
  }
  return RESERVED_TLDS.includes(host.split(".").pop() ?? "");
}

describe("validateEnvironmentUrl 接受完整服务地址", () => {
  it.each([
    "http://echo:8080",
    "https://api.example.test",
    "https://service.example/api/v1",
    // IP＋端口是受支持的写法，地址用 TEST-NET-1（RFC 5737），不指向真实主机。
    "http://192.0.2.10:8080/base",
    "http://[2001:db8::1]:9000/api",
    // 内网短主机名（无点）必须继续支持。
    "http://echo",
    "http://echo:8080/",
    "HTTP://Echo:8080/Api",
    // 基础路径里的百分号编码原样保留，不被 URL 规范化改写。
    "http://echo:8080/a%20b/c",
    "http://my_host.local:8080",
  ])("%s", (raw) => {
    expect(validateEnvironmentUrl(raw)).toBeNull();
  });

  it("IP＋端口继续支持，不能因为“日常用域名”而收窄", () => {
    // 单独锁一遍：删掉 IP／端口支持时这条立刻失败。
    for (const raw of [
      "http://192.0.2.10:8080",
      "https://192.0.2.10",
      "http://192.0.2.10:8080/api/v1",
      "http://10.20.30.40:8080/base",
    ]) {
      expect(validateEnvironmentUrl(raw)).toBeNull();
    }
  });

  it("合法国际化域名不能被前端先拒掉", () => {
    /*
      前端只做早反馈，但把合法地址先拦住同样是缺陷：用户照着真实域名填，界面说它不合法，
      而服务端与目标策略本来就接受这个来源。服务端权威不等于前端可以随便拒。
    */
    for (const raw of [
      "http://例子.测试:8080/api",
      "https://bücher.example/api",
      "http://xn--fsqu00a.xn--0zwm56d:8080/api",
    ]) {
      expect(validateEnvironmentUrl(raw)).toBeNull();
    }
  });

  it("四段全数字的外形必须按 IPv4 判断，不能退回域名分支", () => {
    // 退回域名分支时纯数字标签能通过标签规则，界面会把客户端发不出去的地址说成合法。
    for (const raw of [
      "http://256.256.256.256:8080/api",
      "http://1.2.3.04/api",
      "http://999.1.1.1/",
    ]) {
      expect(validateEnvironmentUrl(raw)).toContain("IPv4");
    }
  });

  it("界面给出的两个示例本身都合法", () => {
    // 示例写错比没有示例更糟：用户照着填反而被拦。
    expect(validateEnvironmentUrl(ENVIRONMENT_URL_EXAMPLE)).toBeNull();
    expect(validateEnvironmentUrl(`${ENVIRONMENT_URL_EXAMPLE}/api/v1`)).toBeNull();
    expect(validateEnvironmentUrl(ENVIRONMENT_URL_IP_EXAMPLE)).toBeNull();
  });

  it("示例地址是保留占位，不含任何真实环境", () => {
    /*
      这里**不比对真实域名的字面量**——把真实地址写进测试，本身就是把它带进了仓库。
      改为校验“主机属于保留占位”这一属性：效果相同，且不引入真实值。
    */
    expect(ENVIRONMENT_URL_EXAMPLE).toBe("https://service.example");
    expect(ENVIRONMENT_URL_IP_EXAMPLE).toBe("http://192.0.2.10:8080");
    expect(isReservedPlaceholder(ENVIRONMENT_URL_EXAMPLE)).toBe(true);
    expect(isReservedPlaceholder(ENVIRONMENT_URL_IP_EXAMPLE)).toBe(true);
  });

  it("保留占位检查本身有效，不是恒真的空断言", () => {
    expect(isReservedPlaceholder("https://intranet.corp.example.com.cn")).toBe(false);
    expect(isReservedPlaceholder("https://10.20.30.40:8080")).toBe(false);
  });
});

describe("validateEnvironmentUrl 拒绝拼不出目标的地址", () => {
  it("缺协议：这是本次要修的主要缺陷", () => {
    // 裸主机名与“主机:端口”过去能存进库，直到执行时才失败，并被误报成白名单问题。
    for (const raw of ["echo", "echo:8080", "target-service:8080", ""]) {
      expect(validateEnvironmentUrl(raw)).toContain("http:// 或 https://");
    }
  });

  it("非 HTTP 协议不猜、不自动补 http://", () => {
    expect(validateEnvironmentUrl("ftp://echo:8080")).toContain("只支持 http 或 https");
    expect(validateEnvironmentUrl("file:///etc/hosts")).toContain("只支持 http 或 https");
    expect(validateEnvironmentUrl(" echo:8080 ")).toContain("http:// 或 https://");
  });

  it("缺主机名", () => {
    expect(validateEnvironmentUrl("http://")).toContain("缺少主机名");
    expect(validateEnvironmentUrl("http:///orders")).toContain("缺少主机名");
  });

  it("畸形与越界端口", () => {
    for (const raw of [
      "http://echo:abc",
      "http://echo:99999",
      "http://echo:0",
      "http://echo:",
      "http://echo:/orders",
      "http://echo:80:90",
    ]) {
      expect(validateEnvironmentUrl(raw)).toContain("端口号");
    }
  });

  it("控制字符、内部空白、反斜杠", () => {
    expect(validateEnvironmentUrl("http://echo/ord\ners")).toContain("控制字符");
    expect(validateEnvironmentUrl("http://echo:8080/a b")).toContain("控制字符");
    expect(validateEnvironmentUrl("http://echo:8080\\a")).toContain("反斜杠");
  });

  it("userinfo、查询串与片段都不是可拼接的基础地址", () => {
    expect(validateEnvironmentUrl("http://user:pass@echo:8080")).toContain("账号信息");
    expect(validateEnvironmentUrl("http://echo:8080/api?token=1")).toContain("查询参数");
    expect(validateEnvironmentUrl("http://echo:8080/api#frag")).toContain("# 片段");
  });

  it("主机名形态不合法", () => {
    expect(validateEnvironmentUrl("http://-bad-:8080")).toContain("主机名不合法");
    expect(validateEnvironmentUrl("http://a..b:8080")).toContain("主机名不合法");
  });
});

describe("validateEnvironmentUrl 的错误信息", () => {
  it("不回显原始地址：粘进来的原文里可能带着秘密", () => {
    const secret = "s3cr3t-token-9f3a";
    for (const raw of [
      `http://echo:8080/path?access_token=${secret}`,
      `http://user:${secret}@echo:8080`,
      `echo:8080?access_token=${secret}`,
    ]) {
      const message = validateEnvironmentUrl(raw);
      expect(message).not.toBeNull();
      expect(message).not.toContain(secret);
      expect(message).not.toContain(raw);
    }
  });

  it("给出可以直接照着填的示例", () => {
    expect(validateEnvironmentUrl("echo:8080")).toContain(ENVIRONMENT_URL_EXAMPLE);
  });
});

describe("validateEnvironmentUrl 只去掉首尾普通空格", () => {
  it("首尾空格不影响判断", () => {
    expect(validateEnvironmentUrl("  http://echo:8080  ")).toBeNull();
  });

  it("首尾的换行不是“普通空格”，照旧报错", () => {
    // trim() 会把换行一起吃掉，与服务端不一致；这里必须与后端同样严格。
    expect(validateEnvironmentUrl("http://echo:8080\n")).toContain("控制字符");
  });
});
