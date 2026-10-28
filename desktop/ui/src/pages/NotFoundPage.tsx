import { ArrowLeft } from "lucide-react";
import { Link } from "react-router-dom";
import { EmptyState } from "../components/PageState";

export function NotFoundPage() {
  return (
    <div className="page not-found-page">
      <EmptyState
        title="没有这个页面"
        detail="地址可能已经变更，或对应资产已不在当前目录。"
        action={
          <Link className="button primary" to="/">
            <ArrowLeft size={15} aria-hidden="true" /> 返回总览
          </Link>
        }
      />
    </div>
  );
}
