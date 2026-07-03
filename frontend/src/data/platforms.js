import {
  AtSign,
  CircleDot,
  Instagram,
  MessageCircle,
  Phone,
  Send,
} from "lucide-react";

export const platforms = [
  { id: "telegram", name: "تلگرام", icon: Send },
  { id: "bale", name: "بله", icon: MessageCircle },
  { id: "rubika", name: "روبیکا", icon: CircleDot },
  { id: "eitaa", name: "ایتا", icon: AtSign },
  { id: "soroush", name: "سروش", icon: MessageCircle },
  { id: "whatsapp", name: "واتساپ", icon: Phone },
  { id: "instagram", name: "اینستاگرام", icon: Instagram },
];

export function getPlatform(platformId) {
  return platforms.find((platform) => platform.id === platformId) ?? platforms[0];
}
