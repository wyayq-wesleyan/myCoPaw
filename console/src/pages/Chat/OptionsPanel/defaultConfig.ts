import type { TFunction } from "i18next";

const defaultConfig = {
  theme: {
    colorPrimary: "#2A63AD",
    darkMode: false,
    prefix: "qwenpaw",
    leftHeader: {
      logo: "",
      title: "汇智小助手",
    },
    bubbleList: {
      userMessageAnchors: {
        variant: "navigator",
      },
    },
  },
  sender: {
    attachments: true,
    maxLength: 100000,
    longTextUpload: {
      enabled: true,
    },
    disclaimer: "懂你所需，伴你左右",
  },
  welcome: {
    greeting: "Hello, how can I help you today?",
    description:
      "I am a helpful assistant that can help you with your questions.",
    avatar: "/huizhi-favicon.svg",
    prompts: [
      {
        value: "Let's start a new journey!",
      },
      {
        value: "Can you tell me what skills you have?",
      },
    ],
  },
  api: {
    baseURL: "",
    token: "",
  },
} as const;

class ChatConfigProvider {
  getGreeting(t: TFunction): string {
    return t("chat.greeting");
  }

  getDescription(t: TFunction): string {
    return t("chat.description");
  }

  getPrompts(t: TFunction): Array<{ value: string }> {
    return [{ value: t("chat.prompt1") }, { value: t("chat.prompt2") }];
  }

  getConfig(t: TFunction) {
    return {
      ...defaultConfig,
      sender: {
        ...defaultConfig.sender,
        disclaimer: t("chat.disclaimer"),
      },
      welcome: {
        ...defaultConfig.welcome,
        greeting: this.getGreeting(t),
        description: this.getDescription(t),
        prompts: this.getPrompts(t),
      },
    };
  }
}

const configProvider = new ChatConfigProvider();

export function getDefaultConfig(t: TFunction) {
  return configProvider.getConfig(t);
}

export default defaultConfig;

export type DefaultConfig = typeof defaultConfig;

// Export provider for extension
export { configProvider };
