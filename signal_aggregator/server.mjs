import serveHotApi from "dailyhot-api";

const port = Number(process.env.PORT || 3210);

await serveHotApi(port);
