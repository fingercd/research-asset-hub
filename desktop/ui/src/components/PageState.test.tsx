import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { ErrorState } from "./PageState";

describe("error recovery", () => {
  it("does not describe a missing collection as an offline API", () => {
    render(<ErrorState error={new ApiError({ status: 404, detail: "No asset exists" })} />);
    expect(screen.getByText("资料或集合不可用")).toBeInTheDocument();
    expect(screen.getByText(/返回列表重新选择/)).toBeInTheDocument();
    expect(screen.queryByText(/启动或恢复本地 API/)).not.toBeInTheDocument();
  });

  it("protects unsaved work when the resource version changed", () => {
    render(<ErrorState error={new ApiError({ status: 412 })} />);
    expect(screen.getByText(/先保留尚未保存/)).toBeInTheDocument();
  });

  it("offers a local retry for a connection failure", () => {
    const retry = vi.fn();
    render(<ErrorState error={new ApiError({ status: 0 })} retry={retry} />);
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.getByText("本机连接需要检查")).toBeInTheDocument();
  });

  it("preserves a caller's context without inventing a network diagnosis", () => {
    render(<ErrorState title="PDF 解析失败" error={new Error("Invalid PDF")} />);
    expect(screen.getByText("PDF 解析失败")).toBeInTheDocument();
    expect(screen.getByText(/可重新加载这项内容/)).toBeInTheDocument();
  });
});
